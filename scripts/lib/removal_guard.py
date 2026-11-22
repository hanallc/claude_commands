#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Check the path against the approved path templates before removal."""
from __future__ import annotations

if __name__ == "__main__":
    with open(__file__[:__file__.rfind("/") + 1] + "package_root.py") as _package_root:
        exec(_package_root.read(), {"__file__": _package_root.name})
    from scripts.lib.removal_guard import main as _main

    raise SystemExit(_main())

import argparse
import hashlib
import hmac
import json
import os
import re
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Generator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Protocol, cast, final

from scripts.buildlog.repository import repo_name
from scripts.lib import conf
from scripts.lib.build_folders import OWNER_MARKER
from scripts.production import staff

PATH_TEMPLATES_KEY = "removal.path_templates"
CARGO_TAG = "CACHEDIR.TAG"
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
LEGACY_TOP = frozenset(("cargo", "slot", "paths", "claude", "hana-ci", "session"))
RESERVED_UNIT_NAMES = frozenset(("main", "ci"))
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


class UnknownMaker(Enum):
    NO_EVIDENCE = "no evidence identifies the maker"


type ManualReviewEvidence = Path | UnknownMaker
type Identity = tuple[int, int]


def identity(info: os.stat_result) -> Identity:
    return info.st_dev, info.st_ino


class RefusalReason(Enum):
    INVALID_ROOT = "unsafe build folder root"
    OUTSIDE_ROOT = "path does not match a removal path template"
    PROTECTED_PATH = "path is or contains the build root or the home folder"
    UNKNOWN_MAKER = UnknownMaker.NO_EVIDENCE.value
    MOUNT = "mount between the build root and the path, at the path, or below it"
    UNVERIFIED_PATH = "cannot verify the path"
    UNAVAILABLE_ROOTS = "cannot read the removal path templates"
    UNAVAILABLE_STAFF = "cannot read the staff record"
    UNAVAILABLE_MOUNTS = "cannot verify the mount table"
    MIRROR_SOURCE = "the mirror output contains source"


class Refused(Exception):
    def __init__(self, reason: RefusalReason, detail: str = "") -> None:
        self.reason: RefusalReason = reason
        detail = " ".join(detail.splitlines())
        super().__init__(f"{reason.value}: {detail}" if detail else reason.value)


class Form(Enum):
    SHORT = ("<repo>", "<unit>", "<short>")
    NUMBER = ("<repo>", "<unit>", "<number>")
    CI = ("<repo>", "ci", "<number>")
    EMPTY_UNIT = ("<repo>", "<unit>")
    EMPTY_REPO = ("<repo>",)
    MAIN = ("<repo>", "main")

    def matches(self, parts: Sequence[str], names: KnownNames) -> bool:
        if (len(parts) != len(self.value) or parts[0].casefold() in LEGACY_TOP
                or not all(NAME.fullmatch(part) and ".." not in part for part in parts)):
            return False
        for token, part in zip(self.value, parts, strict=True):
            match token:
                case "<repo>":
                    allowed = part in names.repositories
                case "<unit>":
                    allowed = part.casefold() in names.units
                case "<number>":
                    allowed = part.isascii() and part.isdecimal()
                case "<short>":
                    allowed = not part.isdecimal()
                case _:
                    allowed = part.casefold() == token
            if not allowed:
                return False
        return True

    @property
    def empty(self) -> bool:
        return self in (Form.EMPTY_UNIT, Form.EMPTY_REPO)


class MirrorOutputForm(Enum):
    TARGET = "mirror target"
    UNIT = "Cargo unit entry"

    @property
    def empty(self) -> bool:
        return False


@dataclass(frozen=True)
class MirrorOutputForms:
    mount_reader: Callable[[], tuple[Path, ...]]

    def match(self, parts: Sequence[str]) -> MirrorOutputForm:
        prefix = len(parts) if len(parts) in (2, 3) else len(parts) - 3
        if (prefix not in (2, 3) or parts[prefix - 1] != "target"
                or not NAME.fullmatch(parts[0]) or ".." in parts[0]
                or prefix == 3 and not re.fullmatch(r"[1-9][0-9]*", parts[1])):
            raise Refused(RefusalReason.OUTSIDE_ROOT)
        if prefix == len(parts):
            return MirrorOutputForm.TARGET
        profile, category, name = parts[prefix:]
        stem = name.split(".", 1)[0]
        if (NAME.fullmatch(profile) and NAME.fullmatch(name) and ".." not in profile and ".." not in name
                and ((category in ("deps", "build", ".fingerprint") and re.fullmatch(r".+-[0-9a-f]{16}", stem))
                     or (category == "incremental" and re.fullmatch(r".+-[a-z0-9]+", name)))):
            return MirrorOutputForm.UNIT
        raise Refused(RefusalReason.OUTSIDE_ROOT)


@dataclass(frozen=True)
class KnownNames:
    repositories: frozenset[str]
    units: frozenset[str]

    def __post_init__(self) -> None:
        names = frozenset(name.casefold() for name in self.units) - RESERVED_UNIT_NAMES
        object.__setattr__(self, "units", names)


def known_names() -> KnownNames:
    try:
        rows = staff.snapshot().all_rows
    except (OSError, staff.StaffRefused) as error:
        raise Refused(RefusalReason.UNAVAILABLE_STAFF, str(error)) from error
    repositories: set[str] = set()
    for row in rows:
        if not isinstance(row.worktree, (staff.PresentWorktree, staff.RemovedWorktree)):
            continue
        if row.kind == "producer":
            repositories.add(row.worktree.path.name.removesuffix("-trunk"))
        if row.kind in ("producer", "unit") and (name := repo_name(str(row.worktree.path))):
            repositories.add(name)
    return KnownNames(frozenset(repositories), frozenset(row.name for row in rows if row.kind == "unit"))


@dataclass(frozen=True)
class Mounts:
    points: frozenset[Identity]
    ancestors: frozenset[Identity]


def mount_snapshot(reader: Callable[[], tuple[Path, ...]]) -> Mounts:
    try:
        paths = tuple(dict.fromkeys(reader()))
        if Path("/") not in paths or any(not point.is_absolute() for point in paths):
            raise ValueError("invalid mount table")
        points: set[Identity] = set()
        ancestors: set[Identity] = set()
        for path in paths:
            try:
                points.add(identity(path.stat()))
            except (PermissionError, FileNotFoundError):
                pass
            for parent in path.parents:
                try:
                    ancestors.add(identity(parent.stat()))
                except (PermissionError, FileNotFoundError):
                    continue
        return Mounts(frozenset(points), frozenset(ancestors))
    except (OSError, RuntimeError, ValueError) as error:
        raise Refused(RefusalReason.UNAVAILABLE_MOUNTS, str(error)) from error


@dataclass(frozen=True)
class CheckedEntry:
    path: Path
    parent: int
    name: str
    info: os.stat_result
    form: Form | MirrorOutputForm
    root: Path
    root_identity: Identity
    ancestors: tuple[tuple[int, str, Identity], ...]
    mount: str
    mount_points: frozenset[Identity] = frozenset()

    def recheck(self) -> None:
        """Verify the same directory entries below the build root."""
        self.recheck_ancestors()
        require_identity(self.parent, self.name, identity(self.info))

    def recheck_ancestors(self) -> None:
        if (identity(self.root.stat()) != self.root_identity
                or isinstance(self.form, MirrorOutputForm) and self.root.resolve(strict=True) != self.root):
            raise ChangedEntry("the build root changed")
        for parent, name, expected in self.ancestors:
            require_identity(parent, name, expected)


def mount_id(descriptor: int) -> str:
    if sys.platform != "linux":
        return ""
    with Path(f"/proc/self/fdinfo/{descriptor}").open() as info:
        for line in info:
            if line.startswith("mnt_id:"):
                return line.partition(":")[2].strip()
    raise Refused(RefusalReason.UNAVAILABLE_MOUNTS, "the descriptor mount ID is missing")


def require_identity(parent: int, name: str, expected: Identity) -> None:
    if identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != expected:
        raise ChangedEntry(f"entry changed before removal: {name}")


def is_real_directory(path: Path) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except (FileNotFoundError, NotADirectoryError):
        return False


def review_evidence(path: Path) -> ManualReviewEvidence:
    """Find a marker to show on a manual review row."""
    sidecar = path.with_name(f".{path.name}{OWNER_MARKER}")
    for marker in (path / OWNER_MARKER, path / CARGO_TAG, path / ".rustc_info.json", sidecar):
        try:
            info = os.lstat(marker)
        except (FileNotFoundError, NotADirectoryError):
            continue
        if stat.S_ISREG(info.st_mode):
            return marker
    return UnknownMaker.NO_EVIDENCE


@dataclass(frozen=True)
class RemovalGuard:
    home: Path
    root: Path
    root_identity: Identity
    protected: frozenset[Identity]
    forms: tuple[Form, ...] | MirrorOutputForms
    names: KnownNames
    mounts: Mounts

    def mirror_mount_points(self) -> frozenset[Identity]:
        if not isinstance(self.forms, MirrorOutputForms):
            raise Refused(RefusalReason.OUTSIDE_ROOT)
        return mount_snapshot(self.forms.mount_reader).points

    def matching_form(self, parts: Sequence[str]) -> Form | MirrorOutputForm:
        if isinstance(self.forms, MirrorOutputForms):
            return self.forms.match(parts)
        if form := next((form for form in self.forms if form.matches(parts, self.names)), None):
            return form
        raise Refused(RefusalReason.OUTSIDE_ROOT)

    def checked_location(self, path: Path) -> tuple[Path, Form | MirrorOutputForm]:
        if not path.is_absolute() or ".." in path.parts:
            raise Refused(RefusalReason.OUTSIDE_ROOT, str(path))
        try:
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(self.root):
                if identity(resolved.stat()) in self.protected:
                    raise Refused(RefusalReason.PROTECTED_PATH)
                raise Refused(RefusalReason.OUTSIDE_ROOT)
            if resolved == self.root:
                raise Refused(RefusalReason.PROTECTED_PATH)
            if path != resolved:
                raise Refused(RefusalReason.UNVERIFIED_PATH, "the requested path has a link or alias")
            return resolved, self.matching_form(resolved.relative_to(self.root).parts)
        except (OSError, RuntimeError, ValueError) as error:
            raise Refused(RefusalReason.UNVERIFIED_PATH, str(error)) from error

    @contextmanager
    def check(self, path: Path) -> Generator[CheckedEntry]:
        """Hold the checked parent until the caller finishes with the entry."""
        resolved, form = self.checked_location(path)
        with ExitStack() as opened:
            if isinstance(self.forms, MirrorOutputForms):
                mounts = mount_snapshot(self.forms.mount_reader)
                root_fd = open_mirror_root(self.home, self.root, mounts, opened)
            else:
                mounts = self.mounts
                root_fd = os.open(self.root, DIRECTORY_FLAGS)
                _ = opened.callback(os.close, root_fd)
            if identity(os.fstat(root_fd)) != self.root_identity:
                raise ChangedEntry("the build root changed")
            parent = root_fd
            root_mount = mount_id(root_fd)
            edges: list[tuple[int, str, Identity]] = []
            parts = resolved.relative_to(self.root).parts
            for index, name in enumerate(parts):
                last = index == len(parts) - 1
                if isinstance(form, MirrorOutputForm) and name not in os.listdir(parent):
                    raise Refused(RefusalReason.UNVERIFIED_PATH, "the entry has an alias")
                flags = FILE_FLAGS if last and form is MirrorOutputForm.UNIT else DIRECTORY_FLAGS
                current = os.open(name, flags, dir_fd=parent)
                _ = opened.callback(os.close, current)
                info = os.fstat(current)
                key = identity(info)
                if (key[0] != self.root_identity[0] or mount_id(current) != root_mount or key in mounts.points
                        or last and key in mounts.ancestors):
                    raise Refused(RefusalReason.MOUNT, str(resolved))
                if key in self.protected:
                    raise Refused(RefusalReason.PROTECTED_PATH)
                if last:
                    if isinstance(form, MirrorOutputForm) and not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                        raise Refused(RefusalReason.UNVERIFIED_PATH, str(resolved))
                    if form is MirrorOutputForm.UNIT and parts[-2] != "deps" and not stat.S_ISDIR(info.st_mode):
                        raise Refused(RefusalReason.OUTSIDE_ROOT, str(resolved))
                    break
                edges.append((parent, name, key))
                parent = current
            else:
                raise Refused(RefusalReason.PROTECTED_PATH)
            if form.empty and os.listdir(current):
                raise Refused(RefusalReason.OUTSIDE_ROOT, "the parent folder is not empty")
            yield CheckedEntry(resolved, parent, name, info, form, self.root, self.root_identity,
                               tuple(edges), root_mount, mounts.points)


FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


def open_mirror_root(home: Path, root: Path, mounts: Mounts, opened: ExitStack) -> int:
    """Open each exact directory below the home folder."""
    parent = os.open(home, DIRECTORY_FLAGS)
    _ = opened.callback(os.close, parent)
    device = os.fstat(parent).st_dev
    mount = mount_id(parent)
    for name in root.relative_to(home).parts:
        if name not in os.listdir(parent):
            raise Refused(RefusalReason.INVALID_ROOT, str(root))
        parent = os.open(name, DIRECTORY_FLAGS, dir_fd=parent)
        _ = opened.callback(os.close, parent)
        key = identity(os.fstat(parent))
        if key[0] != device or key in mounts.points or mount_id(parent) != mount:
            raise Refused(RefusalReason.MOUNT, str(root))
    return parent


def read_mirror(root: Path, *, mount_reader: Callable[[], tuple[Path, ...]] | None = None) -> RemovalGuard:
    """Read the guard for Cargo output in the configured Mac mirror."""
    home = Path.home().resolve(strict=True)
    excluded = (home / "rust", home / ".claude", home / ".cargo")
    if mount_reader is None:
        excluded += (Path("/tmp").resolve(),)
    if (not root.is_relative_to(home) or root == home or ".." in root.parts or root.resolve() != root
            or any(root.is_relative_to(path) or path.is_relative_to(root) for path in excluded)):
        raise Refused(RefusalReason.INVALID_ROOT, str(root))
    if mount_reader is None:
        mount_reader = _mac_mount_points if sys.platform == "darwin" else _linux_mount_points
    mounts = mount_snapshot(mount_reader)
    protected = frozenset(identity(path.stat()) for path in (home, *home.parents))
    with ExitStack() as opened:
        root_key = identity(os.fstat(open_mirror_root(home, root, mounts, opened)))
        root_ancestors = {identity(path.stat()) for path in root.parents}
        for tree in excluded:
            try:
                resolved = tree.resolve(strict=True)
                tree_key = identity(resolved.stat())
            except FileNotFoundError:
                continue
            if (tree_key == root_key or tree_key in root_ancestors
                    or any(identity(parent.stat()) == root_key for parent in resolved.parents)):
                raise Refused(RefusalReason.INVALID_ROOT, str(root))
        if root_key in protected or identity(root.stat()) != root_key:
            raise Refused(RefusalReason.INVALID_ROOT, str(root))
    return RemovalGuard(home, root, root_key, protected, MirrorOutputForms(mount_reader),
                        KnownNames(frozenset(), frozenset()), mounts)


def mirror_targets(guard: RemovalGuard, check_time: Callable[[], None]) -> list[Path]:
    """Find target folders at the two supported mirror depths."""
    if not isinstance(guard.forms, MirrorOutputForms):
        raise Refused(RefusalReason.OUTSIDE_ROOT)
    targets: list[Path] = []
    mounts = mount_snapshot(guard.forms.mount_reader)
    with ExitStack() as opened:
        root_fd = open_mirror_root(guard.home, guard.root, mounts, opened)
        if identity(os.fstat(root_fd)) != guard.root_identity:
            raise ChangedEntry("the mirror root changed")
        mount = mount_id(root_fd)

        def visit(parent: int, folder: Path, depth: int) -> None:
            for name in sorted(os.listdir(parent)):
                check_time()
                if not NAME.fullmatch(name) or ".." in name:
                    continue
                path = folder / name
                if name == "target" and depth in (1, 2):
                    if stat.S_ISDIR(os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode):
                        targets.append(path)
                    continue
                if depth == 2 or depth == 1 and not re.fullmatch(r"[1-9][0-9]*", name):
                    continue
                if not stat.S_ISDIR(os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode):
                    continue
                child = os.open(name, DIRECTORY_FLAGS, dir_fd=parent)
                try:
                    key = identity(os.fstat(child))
                    if key[0] != guard.root_identity[0] or key in mounts.points or mount_id(child) != mount:
                        raise Refused(RefusalReason.MOUNT, str(path))
                    visit(child, path, depth + 1)
                finally:
                    os.close(child)

        visit(root_fd, guard.root, 0)
    return targets


class MirrorStageEntryState(Enum):
    STAGED = "the entry remains in staging"
    ORIGINAL = "the entry is at its original location"
    MISSING = "the entry is absent from both locations"


def observe_mirror_stage(guard: RemovalGuard, parent: Path, parent_key: Identity, stage: str,
                         stage_inode: int, entry: Path, entry_key: Identity) -> MirrorStageEntryState:
    """Find the recorded entry at its original or staged location without following links.

    An entry with a different identity at either location is a new entry, not the recorded one.
    """
    with ExitStack() as opened:
        root = open_mirror_root(guard.home, guard.root, guard.mounts, opened)
        if identity(os.fstat(root)) != guard.root_identity:
            raise ChangedEntry("the mirror root changed")
        anchor = open_mirror_root(guard.home, parent, guard.mounts, opened)
        if identity(os.fstat(anchor)) != parent_key:
            raise ChangedEntry(f"the staging parent changed: {parent}")
        mount = mount_id(anchor)

        def present(parts: tuple[str, ...]) -> bool:
            with ExitStack() as descendants:
                descriptor = anchor
                try:
                    for name in parts[:-1]:
                        child = os.open(name, DIRECTORY_FLAGS, dir_fd=descriptor)
                        _ = descendants.callback(os.close, child)
                        observed = os.fstat(child)
                        if name == stage and observed.st_ino != stage_inode:
                            raise ChangedEntry(f"the staging folder changed: {stage}")
                        if observed.st_dev != parent_key[0] or mount_id(child) != mount:
                            raise Refused(RefusalReason.MOUNT, name)
                        descriptor = child
                    info = os.stat(parts[-1], dir_fd=descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    return False
            return identity(info) == entry_key

        if present((stage, *entry.parts)):
            return MirrorStageEntryState.STAGED
        if present(entry.parts):
            return MirrorStageEntryState.ORIGINAL
        return MirrorStageEntryState.MISSING


@dataclass(frozen=True)
class MirrorEntry:
    key: Identity
    mode: int
    bytes: int
    size: int
    modified: int

    @classmethod
    def from_stat(cls, info: os.stat_result) -> MirrorEntry:
        return cls(identity(info), info.st_mode, info.st_blocks * 512, info.st_size, info.st_mtime_ns)


@dataclass(frozen=True)
class MirrorOutputInventory:
    entries: dict[tuple[str, ...], MirrorEntry]
    bytes: int


def inspect_mirror_output(checked: CheckedEntry, tracked: frozenset[Path],
                          check_time: Callable[[], None], *,
                          staged_folders: tuple[tuple[Path, Identity], ...] = ()) -> MirrorOutputInventory:
    """Verify every entry before the take can move or remove any of them."""
    if not isinstance(checked.form, MirrorOutputForm):
        raise Refused(RefusalReason.OUTSIDE_ROOT)
    selected = tuple(part.casefold() for part in checked.path.parts)
    for path in tracked:
        source = tuple(part.casefold() for part in path.parts)
        shared = min(len(source), len(selected))
        if source[:shared] == selected[:shared]:
            raise Refused(RefusalReason.MIRROR_SOURCE, str(checked.path))
    entries: dict[tuple[str, ...], MirrorEntry] = {}

    def visit(parent: int, name: str, relative: tuple[str, ...]) -> None:
        check_time()
        if name.casefold() in (".git", "cargo.toml"):
            raise Refused(RefusalReason.MIRROR_SOURCE, str(checked.path.joinpath(*relative)))
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if name.startswith(".mac-floor-"):
            if (checked.path.joinpath(*relative), identity(info)) in staged_folders:
                return
            raise ChangedEntry(f"a staged entry requires review: {checked.path.joinpath(*relative)}")
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise Refused(RefusalReason.UNVERIFIED_PATH, str(checked.path.joinpath(*relative)))
        fd = os.open(name, FILE_FLAGS, dir_fd=parent)
        try:
            entry = MirrorEntry.from_stat(os.fstat(fd))
            if entry != MirrorEntry.from_stat(info):
                raise ChangedEntry(f"entry changed during assessment: {name}")
            if (entry.key[0] != checked.root_identity[0] or entry.key in checked.mount_points
                    or mount_id(fd) != checked.mount):
                raise Refused(RefusalReason.MOUNT, str(checked.path.joinpath(*relative)))
            entries[relative] = entry
            if stat.S_ISDIR(entry.mode):
                for child in os.listdir(fd):
                    visit(fd, child, (*relative, child))
        finally:
            os.close(fd)

    checked.recheck()
    visit(checked.parent, checked.name, ())
    checked.recheck()
    return MirrorOutputInventory(entries, sum(entry.bytes for entry in entries.values()))


@contextmanager
def stage_mirror_output(checked: CheckedEntry) -> Generator[CheckedEntry]:
    """Rename a checked entry beside its original location for a second check."""
    from scripts.lib.session_move import rename_exclusive
    from uuid import uuid4

    if not isinstance(checked.form, MirrorOutputForm):
        raise Refused(RefusalReason.OUTSIDE_ROOT)
    stage = ".mac-floor-" + uuid4().hex
    checked.recheck()
    os.mkdir(stage, 0o700, dir_fd=checked.parent)
    checked.recheck()
    stage_info = os.stat(stage, dir_fd=checked.parent, follow_symlinks=False)
    stage_key = identity(stage_info)
    descriptor = os.open(stage, DIRECTORY_FLAGS, dir_fd=checked.parent)
    try:
        if (identity(os.fstat(descriptor)) != stage_key or stage_info.st_uid != os.geteuid()
                or stat.S_IMODE(stage_info.st_mode) & 0o077 or os.listdir(descriptor)):
            raise ChangedEntry("the staging folder changed")
        require_identity(checked.parent, stage, stage_key)
        checked.recheck()
        rename_exclusive(checked.parent, checked.name, descriptor, checked.name)
        staged = replace(checked, parent=descriptor,
                         ancestors=(*checked.ancestors, (checked.parent, stage, stage_key)))
        try:
            os.fsync(descriptor)
            os.fsync(checked.parent)
            staged.recheck()
            yield staged
        finally:
            checked.recheck_ancestors()
            require_identity(checked.parent, stage, stage_key)
            try:
                require_identity(descriptor, checked.name, identity(checked.info))
            except FileNotFoundError:
                pass
            else:
                rename_exclusive(descriptor, checked.name, checked.parent, checked.name)
                os.fsync(descriptor)
            checked.recheck_ancestors()
            require_identity(checked.parent, stage, stage_key)
            os.rmdir(stage, dir_fd=checked.parent)
            os.fsync(checked.parent)
    finally:
        os.close(descriptor)


def _configured_path_templates(value: str) -> tuple[tuple[Path, Form], ...]:
    result: list[tuple[Path, Form]] = []
    for word in value.split():
        path = Path(word).expanduser()
        if not path.is_absolute() or ".." in path.parts or "<repo>" not in path.parts:
            raise ValueError(f"invalid removal path template: {word}")
        prefix = path.parts.index("<repo>")
        result.append((Path(*path.parts[:prefix]), Form(path.parts[prefix:])))
    if not result or len({form for _, form in result}) != len(result):
        raise ValueError("expected distinct removal path templates")
    return tuple(result)


def read(*, mount_reader: Callable[[], tuple[Path, ...]] | None = None,
         build_top: Path | None = None, names: KnownNames | None = None) -> RemovalGuard:
    """Read the path templates and mount table once."""
    setting = conf.read("build_folders").origin(PATH_TEMPLATES_KEY, _configured_path_templates)
    if not isinstance(setting, conf.UsableLiveValue):
        raise Refused(RefusalReason.UNAVAILABLE_ROOTS, setting.reason)
    home = Path.home().resolve()
    top = build_top if build_top is not None else home / "build" if sys.platform == "darwin" else Path("/build")
    if any(root != top for root, _ in setting.value) or top == Path("/") or home.is_relative_to(top):
        raise Refused(RefusalReason.INVALID_ROOT, str(top))
    try:
        resolved = top.resolve(strict=True)
        protected = frozenset(identity(path.stat()) for path in (home, *home.parents))
        info = resolved.stat()
        if top.is_symlink() or identity(info) in protected or resolved == Path("/"):
            raise Refused(RefusalReason.INVALID_ROOT, str(top))
    except (OSError, RuntimeError, ValueError) as error:
        raise Refused(RefusalReason.UNVERIFIED_PATH, str(error)) from error
    if mount_reader is None:
        if sys.platform == "darwin":
            mount_reader = _mac_mount_points
        elif sys.platform == "linux":
            mount_reader = _linux_mount_points
        else:
            raise Refused(RefusalReason.UNAVAILABLE_MOUNTS, sys.platform)
    return RemovalGuard(home, resolved, identity(info), protected,
                        tuple(form for _, form in setting.value), names or known_names(), mount_snapshot(mount_reader))


def remove(checked: CheckedEntry, *, account: Callable[[str, os.stat_result], None] | None = None,
           mirror: MirrorOutputInventory | None = None, check_time: Callable[[], None] | None = None,
           before_unlink: Callable[[Path, os.stat_result], None] | None = None) -> None:
    """Remove the checked entry below the held parent descriptor."""
    if isinstance(checked.form, MirrorOutputForm) and mirror is None:
        raise Refused(RefusalReason.UNVERIFIED_PATH, "the mirror output has no checked inventory")
    parents: list[tuple[int, str, Identity]] = []

    def check_position() -> None:
        if check_time is not None:
            check_time()
        checked.recheck()
        for parent, name, key in parents:
            require_identity(parent, name, key)

    def prepare_unlink(parent: int, name: str, path: Path, before: os.stat_result) -> None:
        if before_unlink is not None:
            before_unlink(path, before)
        check_position()
        require_identity(parent, name, identity(before))

    def remove_name(parent: int, name: str, key: Identity, *, directory: bool) -> None:
        require_identity(parent, name, key)
        if mirror is not None:
            # Check the ancestors after the leaf, so that a move during the leaf check stops the unlink.
            check_position()
            # Check the leaf again, so that the leaf check stays the last check before the unlink.
            require_identity(parent, name, key)
        if directory:
            os.rmdir(name, dir_fd=parent)
        else:
            os.unlink(name, dir_fd=parent)
        if mirror is not None:
            os.fsync(parent)

    def visit(parent: int, name: str, path: Path, before: os.stat_result) -> None:
        key = identity(before)
        if key[0] != checked.info.st_dev or mirror is not None and key in checked.mount_points:
            raise Refused(RefusalReason.MOUNT, str(path))
        if mirror is not None:
            expected = mirror.entries.get(path.relative_to(checked.path).parts)
            if expected != MirrorEntry.from_stat(before):
                raise ChangedEntry(f"mirror entry changed before removal: {path}")
        if stat.S_ISDIR(before.st_mode):
            folder = os.open(name, DIRECTORY_FLAGS, dir_fd=parent)
            try:
                if mount_id(folder) != checked.mount:
                    raise Refused(RefusalReason.MOUNT, str(path))
                if identity(os.fstat(folder)) != key:
                    raise ChangedEntry(f"folder changed before open: {path}")
                parents.append((parent, name, key))
                try:
                    for child in os.listdir(folder):
                        if child.casefold() == ".git":
                            raise ChangedEntry("the folder contains Git metadata")
                        info = os.stat(child, dir_fd=folder, follow_symlinks=False)
                        visit(folder, child, path / child, info)
                finally:
                    _ = parents.pop()
                if mirror is not None:
                    before = os.fstat(folder)
                prepare_unlink(parent, name, path, before)
                remove_name(parent, name, key, directory=True)
            finally:
                os.close(folder)
        else:
            prepare_unlink(parent, name, path, before)
            if mirror is not None or sys.platform == "linux":
                flags = FILE_FLAGS if mirror is not None else os.O_PATH | os.O_NOFOLLOW
                leaf = os.open(name, flags, dir_fd=parent)
                try:
                    pinned = os.fstat(leaf)
                    if (identity(pinned) != key
                            or mirror is not None and MirrorEntry.from_stat(pinned) != MirrorEntry.from_stat(before)):
                        raise ChangedEntry(f"entry changed before open: {path}")
                    if mount_id(leaf) != checked.mount:
                        raise Refused(RefusalReason.MOUNT, str(path))
                    remove_name(parent, name, key, directory=False)
                finally:
                    os.close(leaf)
            else:
                os.unlink(name, dir_fd=parent)
        if account is not None:
            account(str(path), before)

    check_position()
    if checked.form.empty:
        os.rmdir(checked.name, dir_fd=checked.parent)
        if account is not None:
            account(str(checked.path), checked.info)
    else:
        info = (os.stat(checked.name, dir_fd=checked.parent, follow_symlinks=False)
                if mirror is not None else checked.info)
        visit(checked.parent, checked.name, checked.path, info)


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


def _linux_mount_points() -> tuple[Path, ...]:
    points: list[Path] = []
    for line in Path("/proc/self/mountinfo").read_bytes().splitlines():
        fields = line.split()
        if len(fields) < 10 or b"-" not in fields[6:]:
            raise ValueError("invalid Linux mount table entry")
        decoded = re.sub(rb"\\([0-7]{3})", lambda match: bytes([int(match[1], 8)]), fields[4])
        points.append(Path(os.fsdecode(decoded)))
    return tuple(points)



class Tool(Enum):
    TEMPORARY = "tmp_sweep"
    DATACLASS = "tmp_sweep:dataclass"
    NEXTEST = "tmp_sweep:nextest"
    BUILD = "sweep"
    DOC = "sweep:rustdoc"
    CARGO = "scratch:cargo"
    RETIRED_GATE = "gate_folders_delete"


LIST_DIRECTORY = Path(".local/state/removal-lists")


class ListMode(Enum):
    PRINT_ONLY = "print assessment without writing a list"


class ListPublication(Enum):
    PENDING = "the list has no published path"


class ChangedEntry(OSError):
    """The selected entry changed after assessment."""


def _stamp(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_nlink,
            info.st_uid, info.st_gid, info.st_mtime_ns, info.st_ctime_ns)


def fingerprint(path: Path, checked: CheckedEntry | None = None) -> str:
    """Record the identity of each entry without following links."""
    digest = hashlib.sha256()

    def visit(parent: int, name: str, relative: str) -> None:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        digest.update(json.dumps((relative, _stamp(info))).encode())
        if stat.S_ISDIR(info.st_mode):
            descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                if checked is not None and (info.st_dev != checked.info.st_dev or mount_id(descriptor) != checked.mount):
                    raise Refused(RefusalReason.MOUNT, str(path / relative))
                if _stamp(os.fstat(descriptor)) != _stamp(info):
                    raise ChangedEntry("folder changed during assessment")
                with os.scandir(descriptor) as entries:
                    names = sorted(entry.name for entry in entries)
                for child in names:
                    if checked is not None and child.casefold() == ".git":
                        raise ChangedEntry("the folder contains Git metadata")
                    visit(descriptor, child, f"{relative}/{child}")
                if _stamp(os.fstat(descriptor)) != _stamp(info):
                    raise ChangedEntry("folder changed during assessment")
            finally:
                os.close(descriptor)
        if _stamp(os.stat(name, dir_fd=parent, follow_symlinks=False)) != _stamp(info):
            raise ChangedEntry("entry changed during assessment")

    if checked is not None:
        if checked.path != path:
            raise ChangedEntry("the list row has a different path")
        checked.recheck()
        visit(checked.parent, checked.name, ".")
    else:
        parent = os.open(path.parent, DIRECTORY_FLAGS)
        try:
            visit(parent, path.name, ".")
        finally:
            os.close(parent)
    return digest.hexdigest()


@dataclass(frozen=True)
class ListedEntry:
    tool: Tool
    path: Path
    anchor: Path
    size: int
    evidence: ManualReviewEvidence
    reason: str
    fingerprint: str
    assessed_ns: int

    def verify(self, checked: CheckedEntry) -> None:
        if fingerprint(self.path, checked) != self.fingerprint:
            raise ChangedEntry("identity or contents changed since the list was written")

    def line(self) -> str:
        fields: dict[str, object] = {
            "tool": self.tool.value, "path": str(self.path), "bytes": self.size,
            "reason": self.reason, "anchor": str(self.anchor),
            "fingerprint": self.fingerprint, "assessed_ns": self.assessed_ns,
        }
        if isinstance(self.evidence, Path):
            fields["maker"] = str(self.evidence)
        return json.dumps(fields, ensure_ascii=True)

    @staticmethod
    def parse(line: str) -> ListedEntry:
        row = cast(object, json.loads(line))
        if not isinstance(row, dict):
            raise ValueError("expected an object on each list line")
        fields = cast(dict[str, object], row)
        tool, path, anchor, size, evidence, reason, stamp, assessed = (
            fields.get(key) for key in ("tool", "path", "anchor", "bytes", "maker", "reason", "fingerprint", "assessed_ns"))
        if (not isinstance(tool, str) or not isinstance(path, str) or not isinstance(anchor, str)
                or evidence is not None and not isinstance(evidence, str) or not isinstance(reason, str)
                or type(size) is not int or size < 0 or type(assessed) is not int or assessed < 0
                or not isinstance(stamp, str)
                or (stamp != "" and not re.fullmatch(r"[0-9a-f]{64}", stamp))):
            raise ValueError("invalid removal list row")
        selected, parent = Path(path), Path(anchor)
        source = Path(evidence) if isinstance(evidence, str) else UnknownMaker.NO_EVIDENCE
        paths = (selected, parent, source) if isinstance(source, Path) else (selected, parent)
        if any(not value.is_absolute() or ".." in value.parts for value in paths):
            raise ValueError("removal list paths must be absolute")
        return ListedEntry(Tool(tool), selected, parent, size,
                           UnknownMaker.NO_EVIDENCE if stamp else source, reason, stamp, assessed)


@dataclass
class RemovalList:
    """Collect verified entries and publish a readable approval file."""

    tool: Tool
    rows: dict[Path, ListedEntry] = field(default_factory=dict, init=False)
    published: Path | ListPublication = field(default=ListPublication.PENDING, init=False)

    def add_checked(self, checked: CheckedEntry, size: int, reason: str, stamp: str) -> bool:
        """Record a folder that passed the removal guard."""
        return self._add(Tool.BUILD, checked.path, checked.path, size, UnknownMaker.NO_EVIDENCE, reason, stamp)

    def add_manual(self, tool: Tool, path: Path, anchor: Path, size: int, reason: str,
                   evidence: ManualReviewEvidence = UnknownMaker.NO_EVIDENCE) -> bool:
        """Record an entry for which the guard grants no removal authority."""
        return self._add(tool, path, anchor, size, evidence, reason, "")

    def _add(self, tool: Tool, path: Path, anchor: Path, size: int, source: ManualReviewEvidence,
             reason: str, stamp: str) -> bool:
        if path not in self.rows:
            self.rows[path] = ListedEntry(tool, path, anchor, size, source, reason, stamp, time.time_ns())
        return True

    def publish(self) -> Path:
        folder = Path.home() / LIST_DIRECTORY
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=folder,
                                         prefix=f"{self.tool.value}-", suffix=".jsonl", delete=False) as output:
            staged = Path(output.name)
            try:
                for row in self.rows.values():
                    _ = output.write(row.line() + "\n")
                output.flush()
                os.fsync(output.fileno())
                if self.published is ListPublication.PENDING:
                    self.published = staged
                else:
                    _ = staged.replace(self.published)
            except BaseException:
                staged.unlink(missing_ok=True)
                raise
        return self.published

    def report(self) -> None:
        path = self.publish()
        print(f"removal list: {path}")


def single_line(error: BaseException | str) -> str:
    return " ".join(str(error).splitlines())


@dataclass(frozen=True)
class Removed:
    detail: str = ""


@dataclass(frozen=True)
class Kept:
    reason: str


class _BuildRemoval(Protocol):
    def remove_listed_output(self, row: ListedEntry, guard: RemovalGuard) -> Removed | Kept: ...


def _remove_row(row: ListedEntry, guard: RemovalGuard) -> Removed | Kept:
    if row.tool is not Tool.BUILD or not row.fingerprint:
        _ = guard.checked_location(row.path)
        return Kept("the row is for review only")
    from importlib import import_module

    build = cast(_BuildRemoval, cast(object, import_module("scripts.lint.sweep")))
    return build.remove_listed_output(row, guard)


def remove_list(path: Path, approved_hash: str) -> int:
    """Verify the complete list before processing its rows."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("expected a regular removal list file")
            data = stream.read()
        if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), approved_hash):
            raise ValueError("SHA-256 does not match the list")
        rows = tuple(ListedEntry.parse(line) for line in data.decode().splitlines())
        if len({row.path for row in rows}) != len(rows):
            raise ValueError("duplicate paths in the removal list")
    except (OSError, UnicodeError, ValueError, TypeError) as error:
        print(f"removal list refused: {single_line(error)}", file=sys.stderr)
        return 1

    if not rows:
        return 0
    try:
        guard = read()
    except Refused as error:
        if error.reason is RefusalReason.UNAVAILABLE_STAFF:
            print(f"removal list refused: {error}", file=sys.stderr)
            return 1
        for row in rows:
            print(f"kept {str(row.path)!r}: {error}")
        return 0
    for row in rows:
        try:
            result = _remove_row(row, guard)
        except (OSError, Refused, ChangedEntry, RecursionError, ValueError, conf.Unusable) as error:
            result = Kept(single_line(error))
        if isinstance(result, Kept):
            print(f"kept {str(row.path)!r}: {single_line(result.reason)}")
        else:
            print(f"removed {str(row.path)!r}" + (f": {single_line(result.detail)}" if result.detail else ""))
    return 0


class Arguments(argparse.Namespace):
    list_file: Path = Path()
    sha256: str = ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Remove entries from a reviewed list.")
    commands = parser.add_subparsers(required=True)
    remove = commands.add_parser("remove")
    _ = remove.add_argument("--list", dest="list_file", type=Path, required=True)
    _ = remove.add_argument("--sha256", required=True)
    arguments = parser.parse_args(namespace=Arguments())
    return remove_list(arguments.list_file, arguments.sha256)

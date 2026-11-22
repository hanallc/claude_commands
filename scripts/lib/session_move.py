"""Move former Claude scratchpads during cutover."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Generator, Iterator, Sequence
from contextlib import ExitStack, closing, contextmanager, nullcontext
from dataclasses import astuple, dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import cast

from scripts.disks import measure
from scripts.lib import conf, copy_record, scratch, session_folder, transcripts
from scripts.lint import tmp_sweep
from scripts.message import session_records
from scripts.production import staff


CLAUDE_TREE = re.compile(r"claude-[0-9]+")
FOLDER_NAME = re.compile(r"[A-Za-z0-9._-]+")
RENAME_NOREPLACE = 1
RENAME_EXCL = 4
DIRECTORY_OPEN = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
NO_ATIME = os.O_NOATIME if sys.platform == "linux" else 0
JOURNAL = ".session-move"
MOVE_RECORD = "move.json"
STAGED = "staged"
PARTIAL_FILE = "file.partial"
TREE_ROOT = Path()


class MoveRefused(ValueError):
    """A source cannot move without risking its data or another path."""


class LiveSource(MoveRefused):
    """A running session or another process uses the source."""


class JournaledLiveSource(LiveSource):
    """A live source has a saved copy journal."""


class CopyWriteFailed(RuntimeError):
    """A write can have changed the destination or the source."""


@dataclass
class CopyWriteState:
    started: bool = False

    def begin(self) -> None:
        self.started = True


@dataclass
class MoveSummary:
    moved_folders: int = 0
    moved_bytes: int = 0
    kept_live: int = 0
    refused_other: int = 0
    live_copy_pending: bool = False

    def record_move(self, size: int) -> None:
        self.moved_folders += 1
        self.moved_bytes += size

    def keep_live(self, source: Path, reason: LiveSource, *, journaled: bool) -> None:
        kept(source, reason)
        if journaled:
            print(f"session_move: live copy pending at {source}", file=sys.stderr, flush=True)
        self.kept_live += 1
        self.live_copy_pending |= journaled


@dataclass(frozen=True)
class MoveRoots:
    sessions: Path
    former_sessions: Path
    former_temp: Path
    scratch: Path

    @classmethod
    def configured(cls) -> MoveRoots:
        former = session_folder.former_root().absolute()
        return cls(session_folder.configured_root().resolve(), former.resolve(),
                   former.parent.parent.resolve(), copy_record.root().resolve())


class NamingRule(Enum):
    INDEX = ".id index"
    STAFF = "staff record"
    LIVE_CWD = "live session working folder"
    TRANSCRIPT = "transcript working folder"


@dataclass(frozen=True)
class SessionDestination:
    folder: Path
    rule: NamingRule
    session_id: str
    owner: str = ""


def transcript_cwd(session_id: str) -> str:
    paths = session_records.transcript_locations(session_id)
    for path in sorted(paths, key=lambda path: path.stat().st_mtime_ns, reverse=True):
        source = transcripts.transcript_source(path)
        compressed = source.suffix == ".zst"
        cwd = ""
        try:
            with (closing(transcripts.compressed_transcript_lines(source)) if compressed else source.open("rb")) as lines:
                for line in lines:
                    # Read to the end of the archive to check the decoder status.
                    if cwd:
                        continue
                    try:
                        row = cast(object, json.loads(line))
                    except ValueError:
                        continue
                    if isinstance(row, dict):
                        candidate = cast(dict[str, object], row).get("cwd")
                        if isinstance(candidate, str) and Path(candidate).is_absolute():
                            cwd = candidate
                            if not compressed:
                                break
        except FileNotFoundError:
            continue
        except subprocess.CalledProcessError as error:
            raise MoveRefused(f"{source}: zstd decompression failed with status {error.returncode}") from error
        if cwd:
            return cwd
    raise MoveRefused(f"{session_id}: no working folder in a transcript")


def is_directory(path: Path) -> bool:
    try:
        return stat.S_ISDIR(path.lstat().st_mode)
    except FileNotFoundError:
        return False


class DestinationNames:
    """Choose folders from the index, staff records, and launcher rules."""

    def __init__(self, root: Path) -> None:
        self.root: Path = root
        try:
            self.staff: staff.StaffSnapshot = staff.local_snapshot()
        except staff.StaffAbsent:
            self.staff = staff.StaffSnapshot(())
        self.registry: session_records.RegistryRead = session_records.read_registry()
        self.live: dict[str, session_records.SessionRecord] = {
            row["sessionId"]: row for row in reversed(session_records.running_records(self.registry))
        }

    def destination(self, session_id: str, root_fd: int) -> SessionDestination:
        if not session_folder.SESSION_ID.fullmatch(session_id):
            raise MoveRefused(f"invalid session id {session_id!r}")
        index_path = self.root / session_folder.INDEX
        with destination_parent(root_fd, self.root, index_path / session_id, create=False) as index:
            if stat_at(index, session_id) is not None:
                try:
                    name = os.readlink(session_id, dir_fd=index)
                except OSError as error:
                    raise MoveRefused(f"{index_path / session_id}: expected a session index symlink") from error
                indexed = self.root / name
                if not FOLDER_NAME.fullmatch(name) or name.startswith(".") or not is_directory(indexed):
                    raise MoveRefused(f"{index_path / session_id}: unsafe session index target {indexed}")
                return SessionDestination(indexed, NamingRule.INDEX, session_id)
        row = self.staff.by_current_session(session_id) or self.staff.by_member_reference(session_id)
        if row is not None:
            member = session_folder.member_for(row, self.staff)
            cwd, rule = "", NamingRule.STAFF
        else:
            member = None
            live = self.live.get(session_id)
            if live is not None and (cwd := live.get("cwd", "")):
                rule = NamingRule.LIVE_CWD
            else:
                cwd = transcript_cwd(session_id)
                rule = NamingRule.TRANSCRIPT
        base = session_folder.folder_name(member, session_id, cwd)
        owner = member.id if member is not None else session_id
        folder = session_folder.select_folder(self.root, base, session_id)
        return SessionDestination(folder, rule, session_id, owner)

    def remember(self, name: SessionDestination, root_fd: int, mount: int, writes: CopyWriteState) -> None:
        session_id = name.session_id
        creates_index_entry = name.rule is not NamingRule.INDEX
        with child_directory(root_fd, session_folder.INDEX, create=creates_index_entry,
                             mount=mount, on_write=writes.begin) as index:
            if name.rule is NamingRule.INDEX or stat_at(index, session_id) is not None:
                check_index_link(index, session_id, name.folder)
            if name.rule is NamingRule.INDEX:
                return
            record = session_folder.FolderRecord(owner=name.owner, session_id=session_id)
            with child_directory(root_fd, name.folder.name, create=True, mount=mount, on_write=writes.begin) as folder:
                writes.begin()
                write_record_at(folder, session_folder.RECORD, record)
            try:
                os.symlink(name.folder.name, session_id, dir_fd=index)
            except FileExistsError:
                check_index_link(index, session_id, name.folder)
            os.fsync(index)


def check_index_link(index: int, session_id: str, folder: Path) -> None:
    try:
        matches = os.readlink(session_id, dir_fd=index) == folder.name
    except OSError:
        matches = False
    if not matches:
        raise MoveRefused(f"{session_id}: session index entry conflicts with {folder}")


@dataclass(frozen=True)
class ScratchpadLeaf:
    source: Path
    relative: Path


def claude_tree(path: Path) -> bool:
    if CLAUDE_TREE.fullmatch(path.name) is None:
        return False
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()


@dataclass(frozen=True)
class UnrecognizedEntry:
    path: Path
    reason: str
    blocks_cutover: bool


type SourceDiscovery = ScratchpadLeaf | UnrecognizedEntry


def discover_children(folder: Path, visit: Callable[[Path], Iterator[SourceDiscovery]]) -> Iterator[SourceDiscovery]:
    try:
        entries = sorted(folder.iterdir())
    except FileNotFoundError:
        return
    except OSError as error:
        yield UnrecognizedEntry(folder, str(error), True)
        return
    for entry in entries:
        try:
            yield from visit(entry)
        except OSError as error:
            yield UnrecognizedEntry(entry, str(error), True)


def scratchpad_leaves(roots: MoveRoots) -> Iterator[SourceDiscovery]:
    def visit_leaf(leaf: Path) -> Iterator[SourceDiscovery]:
        if session_folder.SESSION_ID.fullmatch(leaf.name) and is_directory(leaf):
            yield ScratchpadLeaf(leaf, Path(*leaf.parts[-3:]))
        else:
            yield UnrecognizedEntry(leaf, "not a Claude session directory", True)

    def visit_project(project: Path) -> Iterator[SourceDiscovery]:
        if not project.name.startswith("-"):
            yield UnrecognizedEntry(project, "outside a Claude project", False)
        elif not is_directory(project):
            yield UnrecognizedEntry(project, "Claude project is not a directory", True)
        else:
            yield from discover_children(project, visit_leaf)

    def visit_tree(tree: Path) -> Iterator[SourceDiscovery]:
        if claude_tree(tree):
            yield from discover_children(tree, visit_project)

    def visit_former_session(session: Path) -> Iterator[SourceDiscovery]:
        if is_directory(session):
            yield from discover_children(session, visit_tree)

    yield from discover_children(roots.former_sessions, visit_former_session)
    yield from discover_children(roots.former_temp, visit_tree)


@dataclass(frozen=True)
class PlannedCopy:
    source: Path
    target: Path
    rule: NamingRule
    session_id: str


def mount_identity(fd: int) -> int:
    """Include bind mounts even when the device number stays the same."""
    if sys.platform == "linux":
        for line in Path(f"/proc/self/fdinfo/{fd}").read_text().splitlines():
            key, _, value = line.partition(":")
            if key == "mnt_id":
                return int(value)
        raise MoveRefused("cannot read directory mount identity")
    return os.fstat(fd).st_dev


def require_mount(fd: int, mount: int, name: str) -> None:
    if mount_identity(fd) != mount:
        raise MoveRefused(f"{name}: entry is on another mount")


@contextmanager
def child_directory(parent: int, name: str, *, create: bool = False, mount: int = -1,
                    on_write: Callable[[], None] = lambda: None) -> Generator[int]:
    if create:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent)
        except FileExistsError:
            pass
        except OSError:
            # The create operation can fail after it changes the folder.
            on_write()
            raise
        else:
            on_write()
            os.fsync(parent)
    fd = os.open(name, DIRECTORY_OPEN, dir_fd=parent)
    try:
        if mount != -1:
            require_mount(fd, mount, name)
        yield fd
    finally:
        os.close(fd)


@contextmanager
def open_directory(path: Path) -> Generator[int]:
    """Open every component without following a symlink."""
    if not path.is_absolute() or ".." in path.parts:
        raise MoveRefused(f"{path}: expected an absolute path within a configured root")
    with ExitStack() as stack:
        root = os.open(path.anchor, DIRECTORY_OPEN)
        _ = stack.callback(os.close, root)
        fd = root
        for name in path.parts[1:]:
            fd = stack.enter_context(child_directory(fd, name))
        yield fd


@contextmanager
def destination_parent(root_fd: int, root: Path, target: Path, *, create: bool,
                       check_write: bool = False) -> Generator[int]:
    fd = root_fd
    path = root
    mount = mount_identity(fd)
    missing = False
    with ExitStack() as stack:
        try:
            for name in target.parent.relative_to(root).parts:
                if name == "..":
                    raise MoveRefused(f"{target}: destination is outside the session root")
                fd = stack.enter_context(child_directory(fd, name, create=create, mount=mount))
                path /= name
        except FileNotFoundError:
            if create:
                raise
            missing = True
        if check_write:
            require_writable_directory(fd, path)
        yield -1 if missing else fd


def stat_at(parent: int, name: str) -> os.stat_result | None:
    if parent != -1:
        try:
            return os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
    return None


def unlink_at(parent: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=parent)
    except FileNotFoundError:
        pass


def temporary_record_name(name: str) -> str:
    return f".{name}.session-move"


def write_record_at(parent: int, name: str, value: object) -> None:
    temporary = temporary_record_name(name)
    unlink_at(parent, temporary)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        _ = output.write(json.dumps(value) + "\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
    os.fsync(parent)


def rename_exclusive(source_parent: int, source: str, target_parent: int, target: str) -> None:
    """Publish through the checked directories and refuse an existing name."""
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        if sys.platform == "darwin":
            rename = cast(Callable[[int, bytes, int, bytes, int], int], libc.renameatx_np)
            result = rename(source_parent, os.fsencode(source), target_parent, os.fsencode(target), RENAME_EXCL)
        elif sys.platform == "linux":
            rename = cast(Callable[[int, bytes, int, bytes, int], int], libc.renameat2)
            result = rename(source_parent, os.fsencode(source), target_parent, os.fsencode(target), RENAME_NOREPLACE)
        else:
            raise MoveRefused("this platform does not support an exclusive rename")
    except AttributeError as error:
        raise MoveRefused("this platform does not support an exclusive rename") from error
    if result != 0:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number), source, None, target)
    os.fsync(target_parent)
    os.fsync(source_parent)


class HashedEntry(Enum):
    FILE = "file"
    LINK = "link"
    FIFO = "fifo"
    DIRECTORY = "directory"


@dataclass(frozen=True)
class PreservedMetadata:
    mode: int
    owner: int
    group: int
    xattrs: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class FileMetadata(PreservedMetadata):
    mtime_ns: int


@dataclass(frozen=True)
class TreeEntry:
    kind: HashedEntry
    size_bytes: int
    metadata: PreservedMetadata
    sha256: str = ""

    def same_content(self, other: TreeEntry) -> bool:
        return (self.kind, self.size_bytes, self.sha256) == (other.kind, other.size_bytes, other.sha256)

    def with_source_xattrs(self, names: frozenset[str]) -> TreeEntry:
        attributes = tuple(pair for pair in self.metadata.xattrs if pair[0] in names)
        if attributes == self.metadata.xattrs:
            return self
        return replace(self, metadata=replace(self.metadata, xattrs=attributes))

    def matches_copy(self, copy: TreeEntry) -> bool:
        """Check the contents and the attributes present in the source."""
        names = frozenset(name for name, _ in self.metadata.xattrs)
        return copy.with_source_xattrs(names) == self


TreeManifest = dict[Path, TreeEntry]


def entry_type(info: os.stat_result, name: str) -> HashedEntry:
    if stat.S_ISDIR(info.st_mode):
        return HashedEntry.DIRECTORY
    if stat.S_ISREG(info.st_mode):
        return HashedEntry.FILE
    if stat.S_ISLNK(info.st_mode):
        return HashedEntry.LINK
    if stat.S_ISFIFO(info.st_mode):
        return HashedEntry.FIFO
    raise MoveRefused(f"{name}: cannot move this file type")


def entry_stamp(info: os.stat_result) -> tuple[int, ...]:
    identity = (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid)
    if stat.S_ISDIR(info.st_mode):
        return identity
    return (*identity, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


@contextmanager
def regular_file(parent: int, name: str, expected: os.stat_result, mount: int) -> Generator[int]:
    fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | NO_ATIME, dir_fd=parent)
    try:
        info = os.fstat(fd)
        require_mount(fd, mount, name)
        if not stat.S_ISREG(info.st_mode) or entry_stamp(info) != entry_stamp(expected):
            raise MoveRefused(f"{name}: entry changed during the check")
        yield fd
        if entry_stamp(os.fstat(fd)) != entry_stamp(expected):
            raise MoveRefused(f"{name}: entry changed during the check")
    finally:
        os.close(fd)


def tree_names(fd: int) -> list[str]:
    reader = os.open(".", DIRECTORY_OPEN | NO_ATIME, dir_fd=fd)
    try:
        return sorted(os.listdir(reader))
    finally:
        os.close(reader)


def tree_entries(fd: int, mount: int, relative: Path = TREE_ROOT) -> Iterator[tuple[Path, int, os.stat_result, HashedEntry]]:
    for name in tree_names(fd):
        path = relative / name
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        kind = entry_type(info, str(path))
        if kind is HashedEntry.DIRECTORY:
            with child_directory(fd, name, mount=mount) as child:
                if entry_stamp(info) != entry_stamp(os.fstat(child)):
                    raise MoveRefused(f"{path}: directory changed during the check")
                yield path, fd, info, kind
                yield from tree_entries(child, mount, path)
        else:
            yield path, fd, info, kind


def check_source_mounts(root: Path, mounts: Sequence[measure.Mount]) -> None:
    for mount in mounts:
        if mount.point == root or mount.point.is_relative_to(root):
            raise MoveRefused(f"{mount.point}: entry is on another mount")


EntryIdentities = dict[Path, os.stat_result]


@dataclass(frozen=True)
class SourceRemoval:
    """Check removal permissions while scanning the source."""

    parent: int
    path: Path


class ReadManifestOnly(Enum):
    NO_REMOVAL_CHECK = "read manifest only"


def require_writable_directory(fd: int, path: Path) -> None:
    if not os.access(".", os.W_OK | os.X_OK, dir_fd=fd, effective_ids=os.access in os.supports_effective_ids):
        raise MoveRefused(f"{path}: directory needs write and search permission")


def require_removable(parent: os.stat_result, entry: os.stat_result, path: Path) -> None:
    if parent.st_mode & stat.S_ISVTX and os.geteuid() not in (0, parent.st_uid, entry.st_uid):
        raise MoveRefused(f"{path}: sticky directory prevents removal")


def preserved_metadata(parent: int, name: str, expected: os.stat_result) -> PreservedMetadata:
    attributes: tuple[tuple[str, str], ...] = ()
    if sys.platform == "linux":
        path = f"/proc/self/fd/{parent}/{name}"
        attributes = tuple(
            (key, os.getxattr(path, key, follow_symlinks=False).hex())
            for key in sorted(os.listxattr(path, follow_symlinks=False))
        )
    current = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if entry_stamp(current) != entry_stamp(expected):
        raise MoveRefused(f"{name}: entry changed while checking metadata")
    mode = stat.S_IMODE(current.st_mode)
    if stat.S_ISDIR(current.st_mode):
        return PreservedMetadata(mode, current.st_uid, current.st_gid, attributes)
    return FileMetadata(mode, current.st_uid, current.st_gid, attributes, current.st_mtime_ns)


def read_entry(parent: int, path: Path, info: os.stat_result, kind: HashedEntry, mount: int) -> TreeEntry:
    name = path.name or "."
    size, sha256 = 0, ""
    if kind is HashedEntry.FILE:
        with regular_file(parent, name, info, mount) as entry:
            with os.fdopen(entry, "rb", closefd=False) as stream:
                size, sha256 = info.st_size, hashlib.file_digest(stream, "sha256").hexdigest()
    elif kind is HashedEntry.LINK:
        target = os.fsencode(os.readlink(name, dir_fd=parent))
        size, sha256 = len(target), hashlib.sha256(target).hexdigest()
    return TreeEntry(kind, size, preserved_metadata(parent, name, info), sha256)


def read_manifest(fd: int, identities: EntryIdentities | None = None, *,
                  removal: SourceRemoval | ReadManifestOnly = ReadManifestOnly.NO_REMOVAL_CHECK) -> TreeManifest:
    mount = mount_identity(fd)
    root = os.fstat(fd)
    checked_parents: dict[Path, os.stat_result] = {}
    if isinstance(removal, SourceRemoval):
        require_writable_directory(removal.parent, removal.path.parent)
        require_removable(os.fstat(removal.parent), root, removal.path)
    manifest = {TREE_ROOT: read_entry(fd, TREE_ROOT, root, HashedEntry.DIRECTORY, mount)}
    if identities is not None:
        identities[TREE_ROOT] = root
    for path, parent, info, kind in tree_entries(fd, mount):
        if isinstance(removal, SourceRemoval):
            if path.parent not in checked_parents:
                require_writable_directory(parent, removal.path / path.parent)
                checked_parents[path.parent] = os.fstat(parent)
            require_removable(checked_parents[path.parent], info, removal.path / path)
        if identities is not None:
            identities[path] = info
        manifest[path] = read_entry(parent, path, info, kind, mount)
    return manifest


def tree_manifest(root: Path) -> TreeManifest:
    with open_directory(root) as fd:
        return read_manifest(fd)


def manifest_digest(manifest: TreeManifest) -> str:
    items = [(str(path), entry.kind.value, entry.size_bytes, entry.sha256, astuple(entry.metadata))
             for path, entry in sorted(manifest.items())]
    return hashlib.sha256(json.dumps(items).encode()).hexdigest()


def copy_metadata(source: int, target: int, info: os.stat_result) -> None:
    existing = os.fstat(target)
    if (existing.st_uid, existing.st_gid) != (info.st_uid, info.st_gid):
        os.fchown(target, info.st_uid, info.st_gid)
    if sys.platform == "linux":
        shutil.copystat(f"/proc/self/fd/{source}", f"/proc/self/fd/{target}")
    else:
        os.fchmod(target, stat.S_IMODE(info.st_mode))
        os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))


def copy_entry(source: int, target: int, name: str, info: os.stat_result, kind: HashedEntry,
               mount: int, journal: int) -> None:
    if kind is HashedEntry.FILE:
        with regular_file(source, name, info, mount) as entry:
            copied = os.open(PARTIAL_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=journal)
            with os.fdopen(entry, "rb", closefd=False) as original, os.fdopen(copied, "wb") as output:
                shutil.copyfileobj(original, output)
                output.flush()
                copy_metadata(entry, copied, info)
                os.fsync(copied)
            os.replace(PARTIAL_FILE, name, src_dir_fd=journal, dst_dir_fd=target)
    else:
        if kind is HashedEntry.LINK:
            os.symlink(os.readlink(name, dir_fd=source), name, dir_fd=target)
        else:
            os.mkfifo(name, mode=stat.S_IMODE(info.st_mode), dir_fd=target)
            os.chmod(name, stat.S_IMODE(info.st_mode), dir_fd=target)
        existing = os.stat(name, dir_fd=target, follow_symlinks=False)
        if (existing.st_uid, existing.st_gid) != (info.st_uid, info.st_gid):
            os.chown(name, info.st_uid, info.st_gid, dir_fd=target, follow_symlinks=False)
        if sys.platform == "linux":
            shutil.copystat(f"/proc/self/fd/{source}/{name}", f"/proc/self/fd/{target}/{name}", follow_symlinks=False)
        else:
            os.utime(name, ns=(info.st_atime_ns, info.st_mtime_ns), dir_fd=target, follow_symlinks=False)


def copy_tree(source: int, staged: int, existing: TreeManifest, expected: TreeManifest,
              journal: int, relative: Path = TREE_ROOT) -> None:
    mount, stage_mount = mount_identity(source), mount_identity(staged)
    for name in tree_names(source):
        path = relative / name
        info = os.stat(name, dir_fd=source, follow_symlinks=False)
        kind = entry_type(info, str(path))
        if kind is HashedEntry.DIRECTORY:
            with (child_directory(source, name, mount=mount) as child,
                  child_directory(staged, name, create=True, mount=stage_mount) as stage_child):
                copy_tree(child, stage_child, existing, expected, journal, path)
        elif path not in expected or expected[path].kind is not kind:
            raise MoveRefused(f"{path}: source changed during the copy")
        elif (staged_entry := existing.get(path)) is None or not expected[path].matches_copy(staged_entry):
            unlink_at(staged, name)
            copy_entry(source, staged, name, info, kind, mount, journal)
    copy_metadata(source, staged, os.fstat(source))
    os.fsync(staged)


class VacantDestination(Enum):
    ENTRY = "vacant destination entry"


@dataclass(frozen=True)
class MoveEntry:
    parent_path: Path
    parent: int
    name: str
    expected: os.stat_result | VacantDestination

    def require_current(self) -> None:
        current = stat_at(self.parent, self.name)
        if isinstance(self.expected, VacantDestination):
            unchanged = current is None
        else:
            unchanged = current is not None and entry_stamp(current) == entry_stamp(self.expected)
        if not unchanged:
            raise MoveRefused(f"{self.parent_path / self.name}: entry changed before the move operation")


def require_final_move(session_id: str, origin: MoveEntry, destination: MoveEntry) -> None:
    require_session_stopped(session_id, session_records.read_registry())
    require_parent_path(origin.parent_path, origin.parent)
    require_parent_path(destination.parent_path, destination.parent)
    origin.require_current()
    destination.require_current()


@dataclass(frozen=True)
class SyncedRetainedFile:
    path: Path
    info: os.stat_result


@dataclass(frozen=True)
class VerifiedPublication:
    move: PlannedCopy
    folder: int
    identities: EntryIdentities
    contents: TreeManifest
    synced_files: dict[tuple[int, int], SyncedRetainedFile] = field(default_factory=dict)

    @contextmanager
    def checked_entry(self, path: Path, *, exact_metadata: bool = False) -> Generator[tuple[int, os.stat_result]]:
        name = path.name or "."
        with destination_parent(self.folder, TREE_ROOT, path, create=False) as parent:
            target = stat_at(parent, name)
            verified = self.identities.get(path)
            if target is None or verified is None or not os.path.samestat(target, verified):
                raise MoveRefused(f"{path}: destination entry changed before deletion")
            actual = read_entry(parent, path, target, entry_type(target, str(path)), mount_identity(parent))
            expected = self.contents[path]
            if not (expected == actual if exact_metadata else expected.matches_copy(actual)):
                raise MoveRefused(f"{path}: destination entry changed before deletion")
            yield parent, target

    def sync_retained_file(self, path: Path, parent: int, info: os.stat_result) -> None:
        with regular_file(parent, path.name, info, mount_identity(parent)) as file:
            os.fsync(file)
        self.synced_files[info.st_dev, info.st_ino] = SyncedRetainedFile(path, info)

    def resync_after_unlink(self, saved: SyncedRetainedFile) -> None:
        with self.checked_entry(saved.path, exact_metadata=True) as (parent, current):
            # Unlink can change the last stamp field, the inode change time.
            if (current.st_nlink != saved.info.st_nlink - 1
                    or entry_stamp(current)[:-1] != entry_stamp(saved.info)[:-1]):
                raise MoveRefused(f"{saved.path}: retained file changed during source unlink")
            self.sync_retained_file(saved.path, parent, current)


@dataclass(frozen=True)
class CopyDeletion:
    root: Path
    source: EntryIdentities
    contents: TreeManifest
    source_targets: dict[Path, Path]
    destination: VerifiedPublication
    before_remove: Callable[[], None] = lambda: None

    def check_source(self, source: int, name: str, path: Path, info: os.stat_result, mount: int) -> Path:
        expected = self.contents.get(path)
        original = self.source.get(path)
        if (expected is None or original is None or not os.path.samestat(info, original)
                or read_entry(source, Path(name), info, entry_type(info, str(path)), mount) != expected):
            raise MoveRefused(f"{path}: source entry changed before deletion")
        return self.source_targets.get(path, path)

    def check_parent_metadata(self, fd: int, path: Path) -> None:
        if preserved_metadata(fd, ".", os.fstat(fd)) != self.contents[path].metadata:
            raise MoveRefused(f"{path}: source directory metadata changed before deletion")


@dataclass(frozen=True)
class MergeStageDeletion:
    root: Path
    entries: dict[Path, MovedSourceEntry | RetainedStageEntry]
    destination: VerifiedPublication
    before_remove: Callable[[], None]

    def check_entry(self, path: Path, entry: TreeEntry) -> None:
        saved = self.entries.get(path)
        if not isinstance(saved, RetainedStageEntry) or saved.stage_sha256 != manifest_digest({path: entry}):
            raise MoveRefused(f"{path}: stage entry differs from the merge journal")

    def check_source(self, source: int, name: str, path: Path, info: os.stat_result, mount: int) -> Path:
        self.check_entry(path, read_entry(source, Path(name), info, entry_type(info, str(path)), mount))
        return path

    def check_parent_metadata(self, fd: int, path: Path) -> None:
        entry = read_entry(fd, TREE_ROOT, os.fstat(fd), HashedEntry.DIRECTORY, mount_identity(fd))
        self.check_entry(path, entry)


@contextmanager
def verified_removal(proof: CopyDeletion | MergeStageDeletion, parent: int, name: str,
                     path: Path, info: os.stat_result, mount: int) -> Generator[tuple[MoveEntry, MoveEntry]]:
    target = proof.check_source(parent, name, path, info, mount)
    origin = MoveEntry((proof.root / path).parent, parent, name, info)
    publication = proof.destination
    with publication.checked_entry(target) as (target_parent, target_info):
        synced = publication.synced_files.get((target_info.st_dev, target_info.st_ino))
        expected = synced.info if synced is not None else target_info
        destination = MoveEntry(publication.move.target / target.parent, target_parent, target.name or ".", expected)
        yield origin, destination


@contextmanager
def checked_removal(proof: CopyDeletion | MergeStageDeletion | None, parent: int, name: str,
                    path: Path, info: os.stat_result, mount: int) -> Generator[None]:
    if proof is None:
        yield
        return
    proof.before_remove()
    if path != TREE_ROOT:
        proof.check_parent_metadata(parent, path.parent)
    publication = proof.destination
    with verified_removal(proof, parent, name, path, info, mount) as (origin, destination):
        synced = publication.synced_files.get((info.st_dev, info.st_ino)) if isinstance(proof, CopyDeletion) else None
        if synced is not None and (info.st_nlink != synced.info.st_nlink
                                   or entry_stamp(info) != entry_stamp(synced.info)):
            raise MoveRefused(f"{path}: entry changed before the move operation")
        require_final_move(publication.move.session_id, origin, destination)
        yield
        if synced is not None:
            publication.resync_after_unlink(synced)


def remove_tree(parent: int, name: str, fd: int, mount: int,
                proof: CopyDeletion | MergeStageDeletion | None = None,
                relative: Path = TREE_ROOT) -> None:
    if not os.path.samestat(os.fstat(fd), os.stat(name, dir_fd=parent, follow_symlinks=False)):
        raise MoveRefused(f"{name}: directory changed before deletion")
    if proof is None:
        os.fchmod(fd, 0o700)
    else:
        with verified_removal(proof, parent, name, relative, os.fstat(fd), mount):
            pass
    for entry in tree_names(fd):
        info = os.stat(entry, dir_fd=fd, follow_symlinks=False)
        path = relative / entry
        if stat.S_ISDIR(info.st_mode):
            with child_directory(fd, entry, mount=mount) as child:
                if entry_stamp(os.fstat(child)) != entry_stamp(info):
                    raise MoveRefused(f"{path}: directory changed before deletion")
                remove_tree(fd, entry, child, mount, proof, path)
        else:
            with checked_removal(proof, fd, entry, path, info, mount):
                os.unlink(entry, dir_fd=fd)
    with checked_removal(proof, parent, name, relative, os.fstat(fd), mount):
        os.rmdir(name, dir_fd=parent)
    os.fsync(parent)


def discard_stage(journal: int, stage: int) -> None:
    unlink_at(journal, PARTIAL_FILE)
    remove_tree(journal, STAGED, stage, mount_identity(journal))
    clear_journal(journal)


type SourceProcessUses = tuple[tuple[Path, int, str], ...]


def process_uses(sources: tuple[Path, ...], session_pids: frozenset[int]) -> SourceProcessUses:
    if not sources:
        return ()
    prefixes = tuple(str(path) for path in sources)
    proc = Path(tmp_sweep.PROC_DIR)
    uses = tmp_sweep.process_paths(str(proc), prefixes) if proc.is_dir() else tmp_sweep.lsof_paths(prefixes)
    if isinstance(uses, tmp_sweep.ProcessUsesUnavailable):
        raise MoveRefused("cannot read process paths")
    unreadable = session_pids.intersection(uses.unreadable_cwd_pids)
    if unreadable:
        raise MoveRefused(f"cannot check the working folder of registered process {min(unreadable)}")
    return tuple((Path(path.removesuffix(tmp_sweep.DELETED_SUFFIX)), pid, how) for path, pid, how in uses.paths)


def require_idle(source: Path, uses: SourceProcessUses) -> None:
    for path, pid, how in uses:
        if path.is_relative_to(source):
            raise LiveSource(f"{source}: process {pid} {how} ({path})")


@dataclass(frozen=True)
class OpenScratchHold:
    path: Path
    owner: str


def open_scratch_holds(path: Path) -> tuple[OpenScratchHold, ...]:
    """Read the ledger and refuse malformed records before moving a tree."""
    try:
        data = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return ()
    if not isinstance(data, dict):
        raise MoveRefused(f"{path}: expected a map of scratch holds")
    holds: list[OpenScratchHold] = []
    for name, record in sorted(cast(dict[str, object], data).items()):
        folder = Path(name)
        if not folder.is_absolute() or ".." in folder.parts or not isinstance(record, dict):
            raise MoveRefused(f"{name}: invalid scratch hold")
        fields = cast(dict[str, object], record)
        ended, owner = fields.get("ended", 0.0), fields.get("maker", "")
        if not isinstance(ended, (int, float)) or not ended >= 0 or not isinstance(owner, str):
            raise MoveRefused(f"{name}: invalid scratch hold fields")
        if ended == 0:
            holds.append(OpenScratchHold(folder, owner or "unknown"))
    return tuple(holds)


@dataclass(frozen=True)
class FolderIdentity:
    device: int
    inode: int

    @classmethod
    def of(cls, info: os.stat_result) -> FolderIdentity:
        return cls(info.st_dev, info.st_ino)

    def matches(self, info: os.stat_result | None) -> bool:
        return info is not None and stat.S_ISDIR(info.st_mode) and self == self.of(info)


def directory_chain(path: Path) -> dict[Path, FolderIdentity]:
    identities: dict[Path, FolderIdentity] = {}
    for folder in (*reversed(path.parents), path):
        try:
            info = folder.stat(follow_symlinks=False)
        except FileNotFoundError:
            break
        if not stat.S_ISDIR(info.st_mode):
            raise MoveRefused(f"{folder}: expected a directory while checking scratch holds")
        identities[folder] = FolderIdentity.of(info)
    return identities


def directory_ancestors(path: Path, chain: dict[Path, FolderIdentity],
                        mounts: Sequence[measure.Mount]) -> set[FolderIdentity]:
    identities = set(chain.values())
    if sys.platform != "linux":
        return identities
    last = next(reversed(chain))
    inode = chain[last]
    device = f"{os.major(inode.device)}:{os.minor(inode.device)}"
    visible = max(((len(mount.point.parts), index, mount) for index, mount in enumerate(mounts)
                   if last.is_relative_to(mount.point)), default=None)
    if visible is None or visible[2].device != device:
        raise MoveRefused(f"{path}: cannot identify its mount")
    mount = visible[2]
    position = mount.root / last.relative_to(mount.point)
    # A bind mount hides its original parents. Check each visible alias of its root.
    for other in mounts:
        if other.device != device or not position.is_relative_to(other.root):
            continue
        alias = Path(other.point / position.relative_to(other.root))
        if alias != last:
            physical = directory_chain(alias)
            if physical.get(alias) == inode:
                identities.update(physical.values())
    return identities


@dataclass(frozen=True)
class CopyInProgress:
    """The source is intact and the stage can contain a partial copy."""


@dataclass(frozen=True)
class CopyFromValidatedSource(CopyInProgress):
    source_manifest: TreeManifest


@dataclass(frozen=True)
class AllCopyAttributes:
    """An earlier journal verifies all attributes in the copy."""


@dataclass(frozen=True)
class SourceAttributeNames:
    """The attributes that the copy must retain at each path."""
    entries: dict[Path, frozenset[str]]

    def in_copy(self, copy: TreeManifest) -> TreeManifest:
        return {path: entry.with_source_xattrs(self.entries.get(path, frozenset()))
                for path, entry in copy.items()}


@dataclass(frozen=True)
class VerifiedCopy:
    folder: FolderIdentity
    sha256: str
    attributes: AllCopyAttributes | SourceAttributeNames

    def source_manifest(self, copy: TreeManifest) -> TreeManifest:
        if isinstance(self.attributes, AllCopyAttributes):
            return copy
        return self.attributes.in_copy(copy)


@dataclass(frozen=True)
class VerifiedStage(VerifiedCopy):
    """The checked stage manifest and identities for publication in this run."""

    contents: TreeManifest
    identities: EntryIdentities


class NewSessionLeaf(Enum):
    ABSENT = "new session leaf"


@dataclass(frozen=True)
class ExistingSessionLeaf:
    folder: FolderIdentity


class MergeOrigin(Enum):
    STAGE = "stage"
    DESTINATION = "destination"


@dataclass(frozen=True)
class MergeRename:
    origin: MergeOrigin
    path: Path
    target: Path
    inode: int


@dataclass(frozen=True)
class MovedSourceEntry:
    target: Path
    source_sha256: str


@dataclass(frozen=True)
class RetainedStageEntry:
    source_sha256: str
    stage_sha256: str


@dataclass(frozen=True)
class MergePlan:
    """The verified stage and the expected result of its journaled renames."""

    folder: FolderIdentity
    stage: FolderIdentity
    sha256: str
    source_bytes: int
    renames: tuple[MergeRename, ...]
    source_entries: dict[Path, MovedSourceEntry | RetainedStageEntry]


@dataclass(frozen=True)
class MoveJournal:
    move: PlannedCopy
    source: FolderIdentity
    state: CopyInProgress | VerifiedCopy | MergePlan
    destination: NewSessionLeaf | ExistingSessionLeaf = NewSessionLeaf.ABSENT

    def save(self, journal: int) -> None:
        move = self.move
        fields: dict[str, object] = {
            "source": str(move.source), "target": str(move.target), "kind": "copy",
            "rule": move.rule.value, "session": move.session_id, "source_id": [self.source.device, self.source.inode],
        }
        if isinstance(self.destination, ExistingSessionLeaf):
            fields["merge_target_id"] = astuple(self.destination.folder)
        if isinstance(self.state, VerifiedCopy):
            fields["verified_id"] = [self.state.folder.device, self.state.folder.inode]
            fields["sha256"] = self.state.sha256
            if isinstance(self.state.attributes, SourceAttributeNames):
                fields["source_xattrs"] = {
                    str(path): sorted(names) for path, names in self.state.attributes.entries.items()
                }
        elif isinstance(self.state, MergePlan):
            fields["merge"] = {
                "stage_id": astuple(self.state.stage),
                "sha256": self.state.sha256,
                "source_bytes": self.state.source_bytes,
                "renames": [[step.origin.value, str(step.path), str(step.target), step.inode]
                            for step in self.state.renames],
                "source_entries": {
                    str(path): (["moved", str(entry.target), entry.source_sha256]
                                if isinstance(entry, MovedSourceEntry)
                                else ["retained", entry.source_sha256, entry.stage_sha256])
                    for path, entry in self.state.source_entries.items()
                },
            }
        write_record_at(journal, MOVE_RECORD, fields)


@dataclass(frozen=True)
class NoPendingMove:
    """There is no journal entry to resume."""


def checked_identity(value: object) -> FolderIdentity:
    if isinstance(value, list):
        numbers = cast(list[object], value)
        if len(numbers) == 2 and isinstance(numbers[0], int) and isinstance(numbers[1], int):
            return FolderIdentity(numbers[0], numbers[1])
    raise MoveRefused("invalid folder identity in the move journal")


def checked_source_xattrs(value: object) -> SourceAttributeNames:
    if not isinstance(value, dict):
        raise MoveRefused("invalid source attributes in the move journal")
    entries: dict[Path, frozenset[str]] = {}
    for path, raw_names in cast(dict[object, object], value).items():
        if not isinstance(path, str) or not isinstance(raw_names, list):
            raise MoveRefused("invalid source attributes in the move journal")
        names = cast(list[object], raw_names)
        if not all(isinstance(name, str) for name in names):
            raise MoveRefused("invalid source attributes in the move journal")
        entries[Path(path)] = frozenset(cast(list[str], names))
    return SourceAttributeNames(entries)


def journal_tree_path(value: object) -> Path:
    if isinstance(value, str):
        path = Path(value)
        if value == str(path) and not path.is_absolute() and ".." not in path.parts and "\0" not in value:
            return path
    raise MoveRefused("invalid relative path in the merge journal")


def journal_digest(value: object) -> str:
    if isinstance(value, str) and re.fullmatch("[0-9a-f]{64}", value):
        return value
    raise MoveRefused("invalid content check in the merge journal")


def read_merge_plan(value: object, destination: ExistingSessionLeaf) -> MergePlan:
    if not isinstance(value, dict):
        raise MoveRefused("invalid merge in the move journal")
    fields = cast(dict[str, object], value)
    renames, entries, size = (fields.get(key) for key in ("renames", "source_entries", "source_bytes"))
    if not isinstance(renames, list) or not isinstance(entries, dict) or not isinstance(size, int) or size < 0:
        raise MoveRefused("invalid merge entries in the move journal")
    steps: list[MergeRename] = []
    for row in cast(list[object], renames):
        if not isinstance(row, list):
            raise MoveRefused("invalid rename in the merge journal")
        parts = cast(list[object], row)
        if len(parts) != 4 or not isinstance(parts[0], str) or not isinstance(parts[3], int):
            raise MoveRefused("invalid rename in the merge journal")
        path, target = journal_tree_path(parts[1]), journal_tree_path(parts[2])
        if path == TREE_ROOT or target == TREE_ROOT or path.parent != target.parent:
            raise MoveRefused("invalid paths in the merge rename")
        steps.append(MergeRename(MergeOrigin(parts[0]), path, target, parts[3]))
    contents: dict[Path, MovedSourceEntry | RetainedStageEntry] = {}
    for path, row in cast(dict[object, object], entries).items():
        if not isinstance(row, list):
            raise MoveRefused("invalid source entry in the merge journal")
        parts = cast(list[object], row)
        if len(parts) != 3:
            raise MoveRefused("invalid source entry in the merge journal")
        state, first, second = parts
        source_path = journal_tree_path(path)
        if state == "moved":
            contents[source_path] = MovedSourceEntry(journal_tree_path(first), journal_digest(second))
        elif state == "retained":
            contents[source_path] = RetainedStageEntry(journal_digest(first), journal_digest(second))
        else:
            raise MoveRefused("invalid source entry state in the merge journal")
    if not isinstance(contents.get(TREE_ROOT), RetainedStageEntry):
        raise MoveRefused("missing stage root proof in the merge journal")
    return MergePlan(destination.folder, checked_identity(fields.get("stage_id")),
                     journal_digest(fields.get("sha256")), size, tuple(steps), contents)


def read_journal(journal: int, roots: MoveRoots) -> MoveJournal | NoPendingMove:
    if stat_at(journal, MOVE_RECORD) is None:
        if any(stat_at(journal, name) is not None for name in (STAGED, PARTIAL_FILE)):
            raise MoveRefused("the staged copy has no journal entry")
        return NoPendingMove()
    fd = os.open(MOVE_RECORD, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=journal)
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        raw = cast(object, json.load(stream))
    if not isinstance(raw, dict):
        raise MoveRefused("invalid move journal")
    fields = cast(dict[str, object], raw)
    source, target, kind, rule, session_id = (fields.get(key) for key in ("source", "target", "kind", "rule", "session"))
    if kind != "copy":
        raise MoveRefused("expected a scratchpad copy in the move journal")
    if not (isinstance(source, str) and isinstance(target, str)
            and isinstance(rule, str) and isinstance(session_id, str)
            and session_folder.SESSION_ID.fullmatch(session_id)):
        raise MoveRefused("invalid paths in the move journal")
    move = PlannedCopy(Path(source), Path(target), NamingRule(rule), session_id)
    if (".." in move.source.parts or ".." in move.target.parts
            or not move.target.is_relative_to(roots.sessions)
            or move.target.name != move.source.name):
        raise MoveRefused("move journal names a path outside the migration")
    relative = move.target.relative_to(roots.sessions).parts
    if not relative or not FOLDER_NAME.fullmatch(relative[0]) or relative[0].startswith("."):
        raise MoveRefused("move journal names an invalid session folder")
    if (len(relative) != 4 or tuple(move.source.parts[-3:]) != relative[1:]
            or move.source.name != session_id or not CLAUDE_TREE.fullmatch(relative[1])
            or not relative[2].startswith("-")
            or not (move.source.parent.parent.parent == roots.former_temp
                    or move.source.parent.parent.parent.parent == roots.former_sessions)):
        raise MoveRefused("invalid scratchpad in the move journal")
    destination = (ExistingSessionLeaf(checked_identity(fields["merge_target_id"]))
                   if "merge_target_id" in fields else NewSessionLeaf.ABSENT)
    digest = fields.get("sha256")
    if "merge" in fields:
        if not isinstance(destination, ExistingSessionLeaf) or fields.keys() & {"sha256", "verified_id", "source_xattrs"}:
            raise MoveRefused("invalid merge state in the move journal")
        state = read_merge_plan(fields["merge"], destination)
    elif isinstance(digest, str) and re.fullmatch("[0-9a-f]{64}", digest):
        attributes = checked_source_xattrs(fields["source_xattrs"]) if "source_xattrs" in fields else AllCopyAttributes()
        state = VerifiedCopy(checked_identity(fields.get("verified_id")), digest, attributes)
    elif not fields.keys() & {"sha256", "verified_id", "source_xattrs"}:
        state = CopyInProgress()
    else:
        raise MoveRefused("invalid copy check in the move journal")
    return MoveJournal(move, checked_identity(fields.get("source_id")), state, destination)


def clear_journal(journal: int) -> None:
    unlink_at(journal, MOVE_RECORD)
    os.fsync(journal)


def require_session_stopped(session_id: str, registry: session_records.RegistryRead) -> None:
    if registry.unreadable:
        raise MoveRefused("cannot read all session records")
    state = session_records.session_liveness(session_id, registry)
    if isinstance(state, session_records.LiveSessionProcess):
        raise LiveSource(f"session {session_id} is live (process {state.pid})")
    if isinstance(state, session_records.SessionLivenessUnknown):
        raise MoveRefused(state.reason)


def check_source_idle(source: Path, registry: session_records.RegistryRead,
                      read_uses: Callable[[], SourceProcessUses]) -> None:
    require_session_stopped(source.name, registry)
    require_idle(source, read_uses())


def check_copy_idle(source: Path, session_pids: frozenset[int]) -> None:
    check_source_idle(source, session_records.read_registry(), lambda: process_uses((source,), session_pids))


@dataclass(frozen=True)
class MergeIdleCheck:
    move: PlannedCopy
    sessions: Path
    session_pids: frozenset[int]

    def __call__(self) -> None:
        require_session_stopped(self.move.session_id, session_records.read_registry())
        paths = (self.move.source, self.move.target, self.sessions / JOURNAL / STAGED)
        # The mover keeps read descriptors open while it checks paths.
        uses = tuple(use for use in process_uses(paths, self.session_pids)
                     if (use[1], use[2]) != (os.getpid(), tmp_sweep.HOLDS_FILE))
        for path in paths:
            require_idle(path, uses)


@contextmanager
def source_parent(roots: MoveRoots, move: PlannedCopy) -> Generator[int]:
    boundary = roots.former_sessions if move.source.is_relative_to(roots.former_sessions) else roots.former_temp
    with (open_directory(boundary) as root,
          destination_parent(root, boundary, move.source, create=False) as parent):
        yield parent


def require_parent_path(path: Path, expected: int) -> None:
    with open_directory(path) as current:
        if (not os.path.samestat(os.fstat(current), os.fstat(expected))
                or mount_identity(current) != mount_identity(expected)):
            raise MoveRefused(f"{path}: parent changed during the move")


def prepare_copy(job: MoveJournal, journal: int, source: int, session_pids: frozenset[int]) -> VerifiedStage:
    move = job.move
    check_copy_idle(move.source, session_pids)
    with (child_directory(source, move.source.name, mount=mount_identity(source)) as original,
          child_directory(journal, STAGED, create=True, mount=mount_identity(journal)) as staged):
        try:
            if not job.source.matches(os.fstat(original)):
                raise MoveRefused("source changed before the copy")
            unlink_at(journal, PARTIAL_FILE)
            expected = (job.state.source_manifest if isinstance(job.state, CopyFromValidatedSource)
                        else read_manifest(original))
            existing = read_manifest(staged)
            copy_tree(original, staged, existing, expected, journal)
            attributes = SourceAttributeNames({
                path: frozenset(name for name, _ in entry.metadata.xattrs)
                for path, entry in expected.items() if entry.metadata.xattrs
            })
            identities: EntryIdentities = {}
            copied = read_manifest(staged, identities)
            if attributes.in_copy(copied) != expected:
                raise MoveRefused("copy check failed (entries, bytes, SHA-256 or metadata)")
        except (OSError, ValueError):
            if job.destination is NewSessionLeaf.ABSENT:
                discard_stage(journal, staged)
            raise
        verified = VerifiedStage(FolderIdentity.of(os.fstat(staged)), manifest_digest(expected),
                                 attributes, copied, identities)
        replace(job, state=verified).save(journal)
        return verified


def merge_suffix(path: Path, reserved: set[Path], name_limit: int) -> Path:
    for number in range(1, len(reserved) + 2):
        suffix = f".session-move.{number}"
        if 0 < name_limit <= len(suffix):
            break
        stem = path.name
        if name_limit > 0:
            stem = os.fsdecode(os.fsencode(stem)[:name_limit - len(suffix)])
        candidate = path.with_name(stem + suffix)
        if candidate not in reserved:
            reserved.add(candidate)
            return candidate
    raise MoveRefused(f"{path}: no name fits the merge suffix")


def plan_merge(verified: VerifiedStage, published: int) -> MergePlan:
    stage_ids = verified.identities
    stage_manifest = verified.contents
    original = verified.source_manifest(stage_manifest)
    target_ids: EntryIdentities = {}
    destination = read_manifest(published, target_ids)
    reserved = set(original) | set(destination)
    limit = os.fpathconf(published, "PC_NAME_MAX")
    target_renames: dict[Path, Path] = {}
    incoming: TreeManifest = {}
    contents: dict[Path, MovedSourceEntry | RetainedStageEntry] = {}
    renames: list[MergeRename] = []
    for path, entry in sorted(original.items()):
        digest = manifest_digest({path: entry})
        if path == TREE_ROOT:
            contents[path] = RetainedStageEntry(digest, manifest_digest({path: stage_manifest[path]}))
            continue
        parent = contents[path.parent]
        if isinstance(parent, MovedSourceEntry):
            placed = parent.target / path.name
        else:
            placed = path
            current = destination.get(path)
            if current is not None:
                if current.same_content(entry):
                    contents[path] = RetainedStageEntry(digest, manifest_digest({path: stage_manifest[path]}))
                    continue
                suffix = merge_suffix(path, reserved, limit)
                if stage_ids[path].st_mtime_ns <= target_ids[path].st_mtime_ns:
                    placed = suffix
                else:
                    target_renames[path] = suffix
                    renames.append(MergeRename(MergeOrigin.DESTINATION, path, suffix, target_ids[path].st_ino))
            renames.append(MergeRename(MergeOrigin.STAGE, path, placed, stage_ids[path].st_ino))
        contents[path] = MovedSourceEntry(placed, digest)
        incoming[placed] = stage_manifest[path]

    target_paths = {TREE_ROOT: TREE_ROOT}
    result = {TREE_ROOT: destination[TREE_ROOT]}
    for path, entry in sorted(destination.items()):
        if path != TREE_ROOT:
            placed = target_renames.get(path, target_paths[path.parent] / path.name)
            target_paths[path] = placed
            result[placed] = entry
    result.update(incoming)
    size = sum(entry.size_bytes for entry in original.values())
    return MergePlan(FolderIdentity.of(os.fstat(published)), verified.folder, manifest_digest(result),
                     size, tuple(renames), contents)


@contextmanager
def merge_rename_parents(step: MergeRename, staged: int, published: int,
                         target_path: Path) -> Generator[tuple[int, int]]:
    origin = staged if step.origin is MergeOrigin.STAGE else published
    with (destination_parent(origin, TREE_ROOT, step.path, create=False, check_write=True) as before,
          destination_parent(published, TREE_ROOT, step.target, create=False, check_write=True) as after):
        if before < 0 or after < 0:
            raise MoveRefused(f"{target_path / step.target}: merge rename parent is missing")
        if mount_identity(before) != mount_identity(after):
            raise MoveRefused(f"{target_path / step.target}: merge rename crosses a mount")
        require_parent_path(target_path / step.target.parent, after)
        yield before, after


@dataclass(frozen=True)
class PendingMergeRename:
    origin: os.stat_result


class CompletedMergeRename(Enum):
    PUBLISHED = "merge entry is published"


def merge_rename_state(step: MergeRename, entry: os.stat_result | None, current: os.stat_result | None,
                       device: int, target_path: Path) -> PendingMergeRename | CompletedMergeRename:
    identity = (device, step.inode)
    if entry is not None and (entry.st_dev, entry.st_ino) == identity:
        if current is None:
            return PendingMergeRename(entry)
    # A later rename can fill a name that an earlier rename vacated.
    elif (current is not None and (current.st_dev, current.st_ino) == identity
          and (entry is None or step.origin is MergeOrigin.DESTINATION)):
        return CompletedMergeRename.PUBLISHED
    raise MoveRefused(f"{target_path / step.target}: merge entry changed before publication")


def preflight_merge_renames(plan: MergePlan, staged: int, published: int, target_path: Path) -> None:
    device = os.fstat(published).st_dev
    simulated: dict[tuple[MergeOrigin, Path], os.stat_result | None] = {}
    for step in plan.renames:
        with merge_rename_parents(step, staged, published, target_path) as (before, after):
            origin = (step.origin, step.path)
            target = (MergeOrigin.DESTINATION, step.target)
            entry = simulated[origin] if origin in simulated else stat_at(before, step.path.name)
            current = simulated[target] if target in simulated else stat_at(after, step.target.name)
            state = merge_rename_state(step, entry, current, device, target_path)
            if isinstance(state, PendingMergeRename):
                require_removable(os.fstat(before), state.origin, target_path / step.path)
                simulated[origin] = None
                simulated[target] = state.origin


def publish_merge_renames(plan: MergePlan, staged: int, published: int, check_idle: MergeIdleCheck) -> None:
    target_path = check_idle.move.target
    stage_path = check_idle.sessions / JOURNAL / STAGED
    device = os.fstat(published).st_dev
    for step in plan.renames:
        with merge_rename_parents(step, staged, published, target_path) as (before, after):
            check_idle()
            entry = stat_at(before, step.path.name)
            current = stat_at(after, step.target.name)
            state = merge_rename_state(step, entry, current, device, target_path)
            if isinstance(state, PendingMergeRename):
                origin_root = stage_path if step.origin is MergeOrigin.STAGE else target_path
                origin = MoveEntry(origin_root / step.path.parent, before, step.path.name, state.origin)
                destination = MoveEntry(target_path / step.target.parent, after, step.target.name,
                                        VacantDestination.ENTRY)
                require_final_move(check_idle.move.session_id, origin, destination)
                rename_exclusive(before, step.path.name, after, step.target.name)
            else:
                os.fsync(after)
                os.fsync(before)


def verify_before_publication(job: MoveJournal, verified: VerifiedCopy, source: int, journal: int) -> VerifiedStage:
    with (child_directory(source, job.move.source.name, mount=mount_identity(source)) as original,
          child_directory(journal, STAGED, mount=mount_identity(journal)) as staged):
        if not job.source.matches(os.fstat(original)):
            raise MoveRefused("source changed before publication")
        if not verified.folder.matches(os.fstat(staged)):
            raise MoveRefused("verified stage changed before publication")
        try:
            identities: EntryIdentities = {}
            copied = read_manifest(staged, identities)
            if (manifest_digest(read_manifest(original)) != verified.sha256
                    or manifest_digest(verified.source_manifest(copied)) != verified.sha256):
                raise MoveRefused("verified copy or source changed before publication")
        except (OSError, ValueError):
            if job.destination is NewSessionLeaf.ABSENT:
                replace(job, state=CopyInProgress()).save(journal)
                discard_stage(journal, staged)
            raise
        return VerifiedStage(verified.folder, verified.sha256, verified.attributes, copied, identities)


def publish_verified_merge(job: MoveJournal, state: VerifiedStage | MergePlan, destination: ExistingSessionLeaf,
                           target: int, journal: int, check_idle: MergeIdleCheck) -> MergePlan:
    move = job.move
    check_idle()
    with (child_directory(target, move.target.name, mount=mount_identity(target)) as published,
          child_directory(journal, STAGED, mount=mount_identity(journal)) as staged):
        if not destination.folder.matches(os.fstat(published)):
            raise MoveRefused(f"destination conflict at {move.target}")
        stage = state.stage if isinstance(state, MergePlan) else state.folder
        if not stage.matches(os.fstat(staged)):
            raise MoveRefused("the verified merge stage changed")
        plan = plan_merge(state, published) if isinstance(state, VerifiedStage) else state
        preflight_merge_renames(plan, staged, published, move.target)
        if isinstance(state, VerifiedStage):
            replace(job, state=plan).save(journal)
        try:
            publish_merge_renames(plan, staged, published, check_idle)
        except LiveSource:
            raise
        except (OSError, MoveRefused) as error:
            raise MoveRefused(f"{error}. Correct the error and rerun session_move to resume the saved merge.") from error
    return plan


def resume_copy(job: MoveJournal, journal: int, roots: MoveRoots, root_fd: int,
                session_pids: frozenset[int]) -> int:
    move = job.move
    check_merge_idle = MergeIdleCheck(move, roots.sessions, session_pids)
    with source_parent(roots, move) as source:
        old = stat_at(source, move.source.name)
        if old is not None and not job.source.matches(old):
            raise MoveRefused("source is no longer the journaled directory")
        with destination_parent(root_fd, roots.sessions, move.target, create=False) as target:
            new = stat_at(target, move.target.name)
        destination = job.destination
        if isinstance(destination, ExistingSessionLeaf):
            require_merge_owner(root_fd, roots.sessions, move, NamingRule.INDEX)
            if not destination.folder.matches(new):
                raise MoveRefused(f"destination conflict at {move.target}")
        elif isinstance(job.state, CopyInProgress) and new is not None:
            raise MoveRefused(f"destination conflict at {move.target}")

        state = job.state
        if isinstance(state, CopyInProgress):
            if old is None:
                raise MoveRefused("source is missing before copy verification")
            state = prepare_copy(job, journal, source, session_pids)

        if isinstance(state, VerifiedCopy) and (new is None or isinstance(destination, ExistingSessionLeaf)):
            if old is None or not state.folder.matches(stat_at(journal, STAGED)):
                raise MoveRefused("a verified copy or its source is missing")
            if isinstance(job.state, VerifiedCopy):
                state = verify_before_publication(job, state, source, journal)

        with destination_parent(root_fd, roots.sessions, move.target, create=new is None) as target:
            if isinstance(destination, ExistingSessionLeaf):
                if old is not None:
                    if not isinstance(state, (VerifiedStage, MergePlan)):
                        raise MoveRefused("the merge stage has not been verified")
                    state = publish_verified_merge(job, state, destination, target, journal, check_merge_idle)
            elif isinstance(state, VerifiedCopy):
                if new is None:
                    check_copy_idle(move.source, session_pids)
                    staged = stat_at(journal, STAGED)
                    if staged is None or not state.folder.matches(staged):
                        raise MoveRefused("the verified stage changed before publication")
                    origin = MoveEntry(roots.sessions / JOURNAL, journal, STAGED, staged)
                    destination_entry = MoveEntry(move.target.parent, target, move.target.name, VacantDestination.ENTRY)
                    require_final_move(move.session_id, origin, destination_entry)
                    rename_exclusive(journal, STAGED, target, move.target.name)
                elif not state.folder.matches(new) or stat_at(journal, STAGED) is not None:
                    raise MoveRefused(f"destination conflict at {move.target}")
                else:
                    os.fsync(target)
                    os.fsync(journal)
            else:
                raise MoveRefused("merge has no journaled destination")
            require_parent_path(move.target.parent, target)
            size = finish_copy(job, state, source, target, journal, check_merge_idle)

        clear_journal(journal)
        return size


def sync_merge_destination(verified: MergePlan, published: VerifiedPublication,
                           target: Path, sessions: Path) -> None:
    # Sync descendants before their containing directories.
    for path, entry in sorted(verified.source_entries.items(), reverse=True):
        if not isinstance(entry, RetainedStageEntry):
            continue
        info = published.identities[path]
        with destination_parent(published.folder, TREE_ROOT, path, create=False) as parent:
            if parent < 0:
                raise MoveRefused(f"{target / path}: parent changed before sync")
            if published.contents[path].kind is HashedEntry.FILE:
                published.sync_retained_file(path, parent, info)
            elif published.contents[path].kind is HashedEntry.DIRECTORY:
                with child_directory(parent, path.name or ".", mount=mount_identity(parent)) as folder:
                    if not os.path.samestat(os.fstat(folder), info):
                        raise MoveRefused(f"{target / path}: directory changed before sync")
                    os.fsync(folder)
    for parent_path in target.parents:
        with open_directory(parent_path) as parent:
            os.fsync(parent)
        if parent_path == sessions:
            break


def finish_copy(job: MoveJournal, verified: VerifiedCopy | MergePlan, source: int, target: int, journal: int,
                check_merge_idle: MergeIdleCheck) -> int:
    move = job.move
    session_pids = check_merge_idle.session_pids
    require_parent_path(move.source.parent, source)
    remaining: TreeManifest = {}
    source_identities: EntryIdentities = {}
    destination_identities: EntryIdentities = {}
    source_present = stat_at(source, move.source.name) is not None
    source_mount = mount_identity(source)
    if source_present:
        check_copy_idle(move.source, session_pids)
        with child_directory(source, move.source.name, mount=source_mount) as original:
            if not job.source.matches(os.fstat(original)):
                raise MoveRefused("source changed before deletion")
            remaining = read_manifest(original, source_identities, removal=SourceRemoval(source, move.source))

    with child_directory(target, move.target.name, mount=mount_identity(target)) as published:
        if not verified.folder.matches(os.fstat(published)):
            raise MoveRefused("published copy changed")
        manifest = read_manifest(published, destination_identities)
        expected = verified.source_manifest(manifest) if isinstance(verified, VerifiedCopy) else manifest
        if manifest_digest(expected) != verified.sha256:
            raise MoveRefused("published copy no longer matches the journal")
        publication = VerifiedPublication(move, published, destination_identities, expected)
        source_targets: dict[Path, Path] = {}
        if isinstance(verified, MergePlan):
            for path, entry in remaining.items():
                saved = verified.source_entries.get(path)
                placed = saved.target if isinstance(saved, MovedSourceEntry) else path
                if (saved is None or manifest_digest({path: entry}) != saved.source_sha256
                        or placed not in expected or not entry.same_content(expected[placed])):
                    raise MoveRefused(f"{path}: source contains data outside the verified merge")
                source_targets[path] = placed
            sync_merge_destination(verified, publication, move.target, check_merge_idle.sessions)
        elif any(expected.get(path) != entry for path, entry in remaining.items()):
            raise MoveRefused("source contains data outside the verified copy")
        if source_present:
            require_parent_path(move.source.parent, source)
            require_parent_path(move.target.parent, target)
            if not verified.folder.matches(stat_at(target, move.target.name)):
                raise MoveRefused("published copy changed before source deletion")
            proof = CopyDeletion(move.source, source_identities, remaining, source_targets, publication)
            if isinstance(verified, MergePlan):
                proof = replace(proof, before_remove=check_merge_idle)
            # Close the source scan before checking for open files.
            check_copy_idle(move.source, session_pids)
            with child_directory(source, move.source.name, mount=source_mount) as original:
                if not job.source.matches(os.fstat(original)):
                    raise MoveRefused("source changed before deletion")
                remove_tree(source, move.source.name, original, source_mount, proof)
        else:
            os.fsync(source)
        if isinstance(verified, MergePlan):
            require_parent_path(move.target.parent, target)
            if not verified.folder.matches(stat_at(target, move.target.name)):
                raise MoveRefused("published merge changed before stage cleanup")
            if stat_at(journal, STAGED) is not None:
                with child_directory(journal, STAGED, mount=mount_identity(journal)) as staged:
                    if not verified.stage.matches(os.fstat(staged)):
                        raise MoveRefused("the verified merge stage changed before cleanup")
                    stage_path = check_merge_idle.sessions / JOURNAL / STAGED
                    stage_proof = MergeStageDeletion(stage_path, verified.source_entries, publication, check_merge_idle)
                    remove_tree(journal, STAGED, staged, mount_identity(journal), stage_proof)
            return verified.source_bytes
        return sum(entry.size_bytes for entry in expected.values())


def require_merge_owner(root_fd: int, root: Path, move: PlannedCopy, rule: NamingRule) -> None:
    folder = root / move.target.relative_to(root).parts[0]
    mount = mount_identity(root_fd)
    with child_directory(root_fd, folder.name, mount=mount) as owner_folder:
        info = stat_at(owner_folder, session_folder.RECORD)
        if info is not None and not stat.S_ISREG(info.st_mode):
            raise MoveRefused(f"destination conflict at {move.target}: invalid session owner record")
        record = session_folder.read_record(folder)
        if info is not None and record is None:
            raise MoveRefused(f"destination conflict at {move.target}: cannot read the session owner record")
    if record is not None and record["session_id"]:
        if record["session_id"] != move.session_id:
            raise MoveRefused(f"destination conflict at {move.target}: folder belongs to session {record['session_id']}")
    elif rule is not NamingRule.INDEX:
        raise MoveRefused(f"destination conflict at {move.target}: folder has no session owner")
    if rule is NamingRule.INDEX:
        with child_directory(root_fd, session_folder.INDEX, mount=mount) as index:
            check_index_link(index, move.session_id, folder)


def check_destination(root_fd: int, root: Path, move: PlannedCopy, name: SessionDestination,
                      mounts: Sequence[measure.Mount]) -> NewSessionLeaf | ExistingSessionLeaf:
    target = move.target
    destination: NewSessionLeaf | ExistingSessionLeaf = NewSessionLeaf.ABSENT
    with destination_parent(root_fd, root, target, create=False, check_write=True) as parent:
        info = stat_at(parent, target.name)
        if info is not None:
            if not stat.S_ISDIR(info.st_mode):
                raise MoveRefused(f"destination conflict at {target}")
            require_merge_owner(root_fd, root, move, name.rule)
            check_source_mounts(target, mounts)
            with child_directory(parent, target.name, mount=mount_identity(parent)) as folder:
                require_writable_directory(folder, target)
                if not os.path.samestat(info, os.fstat(folder)):
                    raise MoveRefused(f"destination changed at {target}")
                destination = ExistingSessionLeaf(FolderIdentity.of(info))
    with destination_parent(root_fd, root, root / JOURNAL / MOVE_RECORD, create=False, check_write=True) as journal:
        check_record_writes(journal, root / JOURNAL, MOVE_RECORD)
    if name.rule is not NamingRule.INDEX:
        with destination_parent(root_fd, root, root / session_folder.INDEX / name.session_id,
                                create=False, check_write=True):
            pass
        with destination_parent(root_fd, root, name.folder / session_folder.RECORD,
                                create=False, check_write=True) as folder:
            check_record_writes(folder, name.folder, session_folder.RECORD)
    return destination


def check_record_writes(parent: int, folder: Path, record: str) -> None:
    for name in (record, temporary_record_name(record)):
        info = stat_at(parent, name)
        if info is not None:
            path = folder / name
            if stat.S_ISDIR(info.st_mode):
                raise MoveRefused(f"{path}: a directory prevents the record write")
            require_removable(os.fstat(parent), info, path)


def show_copy(move: PlannedCopy, size: int) -> None:
    print(f"copy {size} bytes {move.source} -> {move.target} ({move.rule.value})", flush=True)


def kept(source: Path, reason: object) -> None:
    print(f"session_move: kept {source}: {reason}", file=sys.stderr, flush=True)


@dataclass(frozen=True)
class ValidatedCopy:
    move: PlannedCopy
    destination: SessionDestination
    source_identity: FolderIdentity
    source_manifest: TreeManifest
    destination_leaf: NewSessionLeaf | ExistingSessionLeaf

    @property
    def size_bytes(self) -> int:
        return sum(entry.size_bytes for entry in self.source_manifest.values())


def validate_copy(leaf: ScratchpadLeaf, roots: MoveRoots, root_fd: int, names: DestinationNames,
                  mounts: Sequence[measure.Mount], uses: SourceProcessUses) -> ValidatedCopy:
    if leaf.source.is_relative_to(roots.sessions):
        raise MoveRefused("source is inside the destination root")
    check_source_idle(leaf.source, names.registry, lambda: uses)
    name = names.destination(leaf.source.name, root_fd)
    move = PlannedCopy(leaf.source, name.folder / leaf.relative, name.rule, name.session_id)
    with source_parent(roots, move) as source:
        with child_directory(source, move.source.name, mount=mount_identity(source)) as original:
            identity = FolderIdentity.of(os.fstat(original))
            check_source_mounts(move.source, mounts)
            manifest = read_manifest(original, removal=SourceRemoval(source, move.source))
    destination = check_destination(root_fd, roots.sessions, move, name, mounts)
    return ValidatedCopy(move, name, identity, manifest, destination)


def start_copy(validated: ValidatedCopy, names: DestinationNames,
               roots: MoveRoots, root_fd: int, session_pids: frozenset[int]) -> int:
    mount = mount_identity(root_fd)
    move = validated.move
    job = MoveJournal(move, validated.source_identity, CopyFromValidatedSource(validated.source_manifest),
                      validated.destination_leaf)
    writes = CopyWriteState()
    try:
        names.remember(validated.destination, root_fd, mount, writes)
        check_copy_idle(move.source, session_pids)
        with child_directory(root_fd, JOURNAL, create=True, mount=mount, on_write=writes.begin) as journal:
            writes.begin()
            job.save(journal)
            try:
                return resume_copy(job, journal, roots, root_fd, session_pids)
            except LiveSource as error:
                raise JournaledLiveSource(str(error)) from error
    except LiveSource:
        raise
    except (MoveRefused, OSError) as error:
        if writes.started:
            raise CopyWriteFailed(str(error)) from error
        raise


def move_candidates(roots: MoveRoots, root_fd: int, names: DestinationNames, holds: tuple[OpenScratchHold, ...],
                    mounts: Sequence[measure.Mount], summary: MoveSummary, *, dry_run: bool) -> None:
    session_pids = frozenset(row["pid"] for row in names.live.values())
    entries = tuple(scratchpad_leaves(roots))
    sources = {entry.source for entry in entries if isinstance(entry, ScratchpadLeaf)}
    blocked: set[Path] = set()

    with destination_parent(root_fd, roots.sessions, roots.sessions / JOURNAL / MOVE_RECORD, create=False) as journal:
        pending = read_journal(journal, roots)
        if isinstance(pending, MoveJournal):
            sources.add(pending.move.source)
        source_identities: dict[Path, FolderIdentity] = {}
        source_ancestors: dict[Path, set[FolderIdentity]] = {}
        if holds:
            for source in sorted(sources):
                try:
                    chain = directory_chain(source)
                    source_ancestors[source] = directory_ancestors(source, chain, mounts)
                except (MoveRefused, OSError) as error:
                    kept(source, error)
                    summary.refused_other += 1
                    blocked.add(source)
                else:
                    if source in chain:
                        source_identities[source] = chain[source]
        scratch_chain = directory_chain(roots.scratch) if holds else {}
        reported_hold = False
        for hold in holds:
            chain = directory_chain(hold.path)
            ancestors = directory_ancestors(hold.path, chain, mounts)
            held_folder = chain.get(hold.path)
            overlaps = {source for source, source_parents in source_ancestors.items()
                        if source_identities.get(source) in ancestors or held_folder in source_parents}
            if overlaps or scratch_chain.get(roots.scratch) in ancestors:
                kept(hold.path, f"open scratch hold, owner {hold.owner}")
                blocked.update(overlaps)
                summary.refused_other += 1
                reported_hold = True
        if reported_hold:
            print("session_move: an unreleased hold keeps its folder behind. Ask the owner to release it.",
                  file=sys.stderr, flush=True)
        if isinstance(pending, MoveJournal):
            if pending.move.source in blocked:
                return
            print(f"resume copy {pending.move.source} -> {pending.move.target}", flush=True)
            if dry_run:
                summary.refused_other += 1
                return
            try:
                summary.record_move(resume_copy(pending, journal, roots, root_fd, session_pids))
            except LiveSource as error:
                summary.keep_live(pending.move.source, error, journaled=True)
                return
            except (MoveRefused, OSError):
                print(f"session_move: saved copy failed at {pending.move.source}", file=sys.stderr, flush=True)
                raise
            sources.discard(pending.move.source)

    eligible = sources - blocked
    leaves = tuple(entry for entry in entries if isinstance(entry, ScratchpadLeaf) and entry.source in eligible)
    for entry in entries:
        if isinstance(entry, UnrecognizedEntry):
            kept(entry.path, entry.reason)
            summary.refused_other += int(entry.blocks_cutover)

    uses = process_uses(tuple(leaf.source for leaf in leaves), session_pids)
    for leaf in leaves:
        try:
            validated = validate_copy(leaf, roots, root_fd, names, mounts, uses)
            size = validated.size_bytes
            show_copy(validated.move, size)
            if not dry_run:
                size = start_copy(validated, names, roots, root_fd, session_pids)
            summary.record_move(size)
        except (MoveRefused, OSError, CopyWriteFailed) as error:
            if isinstance(error, LiveSource):
                summary.keep_live(leaf.source, error, journaled=isinstance(error, JournaledLiveSource))
            else:
                kept(leaf.source, error)
                summary.refused_other += 1
            if isinstance(error, CopyWriteFailed):
                print(f"session_move: copy write failed at {leaf.source}", file=sys.stderr, flush=True)
            if isinstance(error, (JournaledLiveSource, CopyWriteFailed)):
                return


def moves(roots: MoveRoots, summary: MoveSummary, *, dry_run: bool) -> None:
    if any(roots.sessions.is_relative_to(path) or path.is_relative_to(roots.sessions)
           for path in (roots.scratch, roots.former_sessions)):
        raise MoveRefused("the session root overlaps a source root")
    scratch_settings = conf.read("scratch")
    holds_path = scratch.holds_file(scratch_settings=scratch_settings)
    with (open_directory(roots.sessions) as root,
          nullcontext() if dry_run else session_folder.root_lock(roots.sessions),
          nullcontext() if dry_run else scratch.locked_holds(scratch_settings=scratch_settings)):
        move_candidates(
            roots, root, DestinationNames(roots.sessions), open_scratch_holds(holds_path),
            measure.read_mounts(), summary, dry_run=dry_run,
        )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--dry-run", action="store_true", help="Print the moves without changing any source or target.")
    arguments = parser.parse_args(argv)
    dry_run = cast(bool, arguments.dry_run)
    summary = MoveSummary()
    try:
        moves(MoveRoots.configured(), summary, dry_run=dry_run)
    except (OSError, ValueError) as error:
        print(f"session_move: {error}", file=sys.stderr)
        summary.refused_other += 1
    action = "would move" if dry_run else "moved"
    print(f"session_move: {action} {summary.moved_folders} folders ({summary.moved_bytes / 2**30:.2f} GiB), "
          + f"kept {summary.kept_live} live, refused {summary.refused_other} other", flush=True)
    return int(summary.refused_other > 0 or summary.live_copy_pending)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

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
from dataclasses import astuple, dataclass, replace
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

    def destination(self, session_id: str) -> SessionDestination:
        if not session_folder.SESSION_ID.fullmatch(session_id):
            raise MoveRefused(f"invalid session id {session_id!r}")
        index_path = self.root / session_folder.INDEX
        with open_directory(index_path, missing_ok=True) as index:
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

    def remember(self, name: SessionDestination, root_fd: int) -> None:
        session_id = name.session_id
        mount = mount_identity(root_fd)
        with child_directory(root_fd, session_folder.INDEX, create=True, mount=mount) as index:
            if name.rule is NamingRule.INDEX or stat_at(index, session_id) is not None:
                check_index_link(index, session_id, name.folder)
            if name.rule is NamingRule.INDEX:
                return
            record = session_folder.FolderRecord(owner=name.owner, session_id=session_id)
            with child_directory(root_fd, name.folder.name, create=True, mount=mount) as folder:
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


def children(folder: Path) -> list[Path]:
    try:
        return sorted(folder.iterdir())
    except FileNotFoundError:
        return []


def claude_tree(path: Path) -> bool:
    return (CLAUDE_TREE.fullmatch(path.name) is not None and is_directory(path)
            and path.lstat().st_uid == os.getuid())


@dataclass(frozen=True)
class UnrecognizedEntry:
    path: Path
    reason: str
    blocks_cutover: bool


def scratchpad_leaves(roots: MoveRoots) -> Iterator[ScratchpadLeaf | UnrecognizedEntry]:
    legacy = (entry for entry in children(roots.former_temp) if claude_tree(entry))
    former = (entry for session in children(roots.former_sessions) if is_directory(session)
              for entry in children(session) if claude_tree(entry))
    for tree in (*former, *legacy):
        for project in children(tree):
            if not project.name.startswith("-"):
                yield UnrecognizedEntry(project, "outside a Claude project", False)
                continue
            if not is_directory(project):
                yield UnrecognizedEntry(project, "Claude project is not a directory", True)
                continue
            for leaf in children(project):
                if session_folder.SESSION_ID.fullmatch(leaf.name) and is_directory(leaf):
                    yield ScratchpadLeaf(leaf, Path(tree.name) / project.name / leaf.name)
                else:
                    yield UnrecognizedEntry(leaf, "not a Claude session directory", True)


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
def child_directory(parent: int, name: str, *, create: bool = False, mount: int = -1) -> Generator[int]:
    if create:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent)
        except FileExistsError:
            pass
        else:
            os.fsync(parent)
    fd = os.open(name, DIRECTORY_OPEN, dir_fd=parent)
    try:
        if mount != -1:
            require_mount(fd, mount, name)
        yield fd
    finally:
        os.close(fd)


@contextmanager
def open_directory(path: Path, *, missing_ok: bool = False) -> Generator[int]:
    """Open every component without following a symlink."""
    if not path.is_absolute() or ".." in path.parts:
        raise MoveRefused(f"{path}: expected an absolute path within a configured root")
    with ExitStack() as stack:
        root = os.open(path.anchor, DIRECTORY_OPEN)
        _ = stack.callback(os.close, root)
        fd = root
        try:
            for name in path.parts[1:]:
                fd = stack.enter_context(child_directory(fd, name))
        except FileNotFoundError:
            if not missing_ok:
                raise
            fd = -1
        yield fd


@contextmanager
def destination_parent(root_fd: int, root: Path, target: Path, *, create: bool) -> Generator[int]:
    fd = root_fd
    mount = mount_identity(fd)
    with ExitStack() as stack:
        try:
            for name in target.parent.relative_to(root).parts:
                if name == "..":
                    raise MoveRefused(f"{target}: destination is outside the session root")
                fd = stack.enter_context(child_directory(fd, name, create=create, mount=mount))
        except FileNotFoundError:
            if create:
                raise
            fd = -1
        yield fd


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


def write_record_at(parent: int, name: str, value: object) -> None:
    temporary = f".{name}.session-move"
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


def tree_size(root: Path, mounts: Sequence[measure.Mount]) -> int:
    for mount in mounts:
        if mount.point == root or mount.point.is_relative_to(root):
            raise MoveRefused(f"{mount.point}: entry is on another mount")
    with open_directory(root) as fd:
        return sum(info.st_size for _, _, info, kind in tree_entries(fd, mount_identity(fd))
                   if kind is not HashedEntry.DIRECTORY)


EntryIdentities = dict[Path, os.stat_result]


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


def read_manifest(fd: int, identities: EntryIdentities | None = None) -> TreeManifest:
    mount = mount_identity(fd)
    root = os.fstat(fd)
    manifest = {TREE_ROOT: read_entry(fd, TREE_ROOT, root, HashedEntry.DIRECTORY, mount)}
    if identities is not None:
        identities[TREE_ROOT] = root
    for path, parent, info, kind in tree_entries(fd, mount):
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


@dataclass(frozen=True)
class CopyDeletion:
    source: EntryIdentities
    destination: EntryIdentities
    contents: TreeManifest
    folder: int

    def check(self, source: int, path: Path, info: os.stat_result, mount: int) -> None:
        name = path.name or "."
        expected = self.contents.get(path)
        original = self.source.get(path)
        if (expected is None or original is None or not os.path.samestat(info, original)
                or read_entry(source, path, info, entry_type(info, str(path)), mount) != expected):
            raise MoveRefused(f"{path}: source entry changed before deletion")
        with destination_parent(self.folder, TREE_ROOT, path, create=False) as parent:
            target = stat_at(parent, name)
            verified = self.destination.get(path)
            if (target is None or verified is None or not os.path.samestat(target, verified)
                    or not expected.matches_copy(read_entry(parent, path, target, entry_type(target, str(path)),
                                                            mount_identity(parent)))):
                raise MoveRefused(f"{path}: destination entry changed before deletion")
            if entry_stamp(os.stat(name, dir_fd=parent, follow_symlinks=False)) != entry_stamp(target):
                raise MoveRefused(f"{path}: destination entry changed during the deletion check")
        if entry_stamp(os.stat(name, dir_fd=source, follow_symlinks=False)) != entry_stamp(info):
            raise MoveRefused(f"{path}: source entry changed during the deletion check")

    def check_parent_metadata(self, fd: int, path: Path) -> None:
        if preserved_metadata(fd, ".", os.fstat(fd)) != self.contents[path].metadata:
            raise MoveRefused(f"{path}: source directory metadata changed before deletion")


def remove_tree(parent: int, name: str, fd: int, mount: int, proof: CopyDeletion | None = None,
                relative: Path = TREE_ROOT) -> None:
    if not os.path.samestat(os.fstat(fd), os.stat(name, dir_fd=parent, follow_symlinks=False)):
        raise MoveRefused(f"{name}: directory changed before deletion")
    if proof is None:
        os.fchmod(fd, 0o700)
    else:
        proof.check(fd if relative == TREE_ROOT else parent, relative, os.fstat(fd), mount)
    for entry in tree_names(fd):
        info = os.stat(entry, dir_fd=fd, follow_symlinks=False)
        path = relative / entry
        if stat.S_ISDIR(info.st_mode):
            with child_directory(fd, entry, mount=mount) as child:
                if entry_stamp(os.fstat(child)) != entry_stamp(info):
                    raise MoveRefused(f"{path}: directory changed before deletion")
                remove_tree(fd, entry, child, mount, proof, path)
        else:
            if proof is not None:
                proof.check(fd, path, info, mount)
                proof.check_parent_metadata(fd, relative)
            os.unlink(entry, dir_fd=fd)
    if proof is not None:
        proof.check(fd if relative == TREE_ROOT else parent, relative, os.fstat(fd), mount)
        if relative != TREE_ROOT:
            proof.check_parent_metadata(parent, relative.parent)
        if not os.path.samestat(os.fstat(fd), os.stat(name, dir_fd=parent, follow_symlinks=False)):
            raise MoveRefused(f"{name}: directory changed before deletion")
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
class MoveJournal:
    move: PlannedCopy
    source: FolderIdentity
    state: CopyInProgress | VerifiedCopy

    def save(self, journal: int) -> None:
        move = self.move
        fields: dict[str, object] = {
            "source": str(move.source), "target": str(move.target), "kind": "copy",
            "rule": move.rule.value, "session": move.session_id, "source_id": [self.source.device, self.source.inode],
        }
        if isinstance(self.state, VerifiedCopy):
            fields["verified_id"] = [self.state.folder.device, self.state.folder.inode]
            fields["sha256"] = self.state.sha256
            if isinstance(self.state.attributes, SourceAttributeNames):
                fields["source_xattrs"] = {
                    str(path): sorted(names) for path, names in self.state.attributes.entries.items()
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
    digest = fields.get("sha256")
    if isinstance(digest, str) and re.fullmatch("[0-9a-f]{64}", digest):
        attributes = checked_source_xattrs(fields["source_xattrs"]) if "source_xattrs" in fields else AllCopyAttributes()
        state = VerifiedCopy(checked_identity(fields.get("verified_id")), digest, attributes)
    elif not fields.keys() & {"sha256", "verified_id", "source_xattrs"}:
        state = CopyInProgress()
    else:
        raise MoveRefused("invalid copy check in the move journal")
    return MoveJournal(move, checked_identity(fields.get("source_id")), state)


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


def prepare_copy(job: MoveJournal, journal: int, source: int, session_pids: frozenset[int]) -> VerifiedCopy:
    move = job.move
    check_copy_idle(move.source, session_pids)
    with (child_directory(source, move.source.name, mount=mount_identity(source)) as original,
          child_directory(journal, STAGED, create=True, mount=mount_identity(journal)) as staged):
        try:
            if not job.source.matches(os.fstat(original)):
                raise MoveRefused("source changed before the copy")
            unlink_at(journal, PARTIAL_FILE)
            expected = read_manifest(original)
            existing = read_manifest(staged)
            copy_tree(original, staged, existing, expected, journal)
            attributes = SourceAttributeNames({
                path: frozenset(name for name, _ in entry.metadata.xattrs)
                for path, entry in expected.items() if entry.metadata.xattrs
            })
            if attributes.in_copy(read_manifest(staged)) != expected:
                raise MoveRefused("copy check failed (entries, bytes, SHA-256 or metadata)")
        except (OSError, ValueError):
            discard_stage(journal, staged)
            raise
        verified = VerifiedCopy(FolderIdentity.of(os.fstat(staged)), manifest_digest(expected), attributes)
        replace(job, state=verified).save(journal)
        return verified


def resume_copy(job: MoveJournal, journal: int, roots: MoveRoots, root_fd: int,
                session_pids: frozenset[int]) -> int:
    move = job.move
    with source_parent(roots, move) as source:
        old = stat_at(source, move.source.name)
        if old is not None and not job.source.matches(old):
            raise MoveRefused("source is no longer the journaled directory")
        with destination_parent(root_fd, roots.sessions, move.target, create=False) as target:
            new = stat_at(target, move.target.name)
        if isinstance(job.state, CopyInProgress):
            if new is not None:
                raise MoveRefused(f"destination conflict at {move.target}")
            if old is None:
                raise MoveRefused("source is missing before copy verification")
            verified = prepare_copy(job, journal, source, session_pids)
        else:
            verified = job.state

        with destination_parent(root_fd, roots.sessions, move.target, create=new is None) as target:
            if new is None:
                if old is None or not verified.folder.matches(stat_at(journal, STAGED)):
                    raise MoveRefused("a verified copy or its source is missing")
                if isinstance(job.state, VerifiedCopy):
                    with (child_directory(source, move.source.name, mount=mount_identity(source)) as original,
                          child_directory(journal, STAGED, mount=mount_identity(journal)) as staged):
                        if not job.source.matches(os.fstat(original)):
                            raise MoveRefused("source changed before publication")
                        try:
                            if (manifest_digest(read_manifest(original)) != verified.sha256
                                    or manifest_digest(verified.source_manifest(read_manifest(staged))) != verified.sha256):
                                raise MoveRefused("verified copy or source changed before publication")
                        except (OSError, ValueError):
                            replace(job, state=CopyInProgress()).save(journal)
                            discard_stage(journal, staged)
                            raise
                check_copy_idle(move.source, session_pids)
                require_parent_path(move.target.parent, target)
                rename_exclusive(journal, STAGED, target, move.target.name)
            elif not verified.folder.matches(new) or stat_at(journal, STAGED) is not None:
                raise MoveRefused(f"destination conflict at {move.target}")
            else:
                os.fsync(target)
                os.fsync(journal)

            require_parent_path(move.target.parent, target)
            size = finish_copy(job, verified, source, target, session_pids)
        clear_journal(journal)
        return size


def finish_copy(job: MoveJournal, verified: VerifiedCopy, source: int, target: int,
                session_pids: frozenset[int]) -> int:
    move = job.move
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
            remaining = read_manifest(original, source_identities)

    with child_directory(target, move.target.name, mount=mount_identity(target)) as published:
        if not verified.folder.matches(os.fstat(published)):
            raise MoveRefused("published copy changed")
        expected = verified.source_manifest(read_manifest(published, destination_identities))
        if manifest_digest(expected) != verified.sha256:
            raise MoveRefused("published copy no longer matches the journal")
        if any(expected.get(path) != entry for path, entry in remaining.items()):
            raise MoveRefused("source contains data outside the verified copy")
        if source_present:
            require_parent_path(move.source.parent, source)
            require_parent_path(move.target.parent, target)
            if not verified.folder.matches(stat_at(target, move.target.name)):
                raise MoveRefused("published copy changed before source deletion")
            proof = CopyDeletion(source_identities, destination_identities, expected, published)
            # Close the source scan before checking for open files.
            check_copy_idle(move.source, session_pids)
            with child_directory(source, move.source.name, mount=source_mount) as original:
                if not job.source.matches(os.fstat(original)):
                    raise MoveRefused("source changed before deletion")
                remove_tree(source, move.source.name, original, source_mount, proof)
        else:
            os.fsync(source)
        return sum(entry.size_bytes for entry in expected.values())


def check_destination(root_fd: int, root: Path, target: Path, destinations: set[Path]) -> None:
    with destination_parent(root_fd, root, target, create=False) as parent:
        if target in destinations or stat_at(parent, target.name) is not None:
            raise MoveRefused(f"destination conflict at {target}")


def show_copy(move: PlannedCopy, size: int) -> None:
    print(f"copy {size} bytes {move.source} -> {move.target} ({move.rule.value})", flush=True)


def kept(source: Path, reason: object) -> None:
    print(f"session_move: kept {source}: {reason}", file=sys.stderr, flush=True)


def start_copy(move: PlannedCopy, roots: MoveRoots, root_fd: int, session_pids: frozenset[int]) -> int:
    with (child_directory(root_fd, JOURNAL, create=True, mount=mount_identity(root_fd)) as journal,
          source_parent(roots, move) as source):
        with child_directory(source, move.source.name, mount=mount_identity(source)) as original:
            identity = FolderIdentity.of(os.fstat(original))
        job = MoveJournal(move, identity, CopyInProgress())
        job.save(journal)
        return resume_copy(job, journal, roots, root_fd, session_pids)


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
        source_chains = {source: directory_chain(source) for source in sources} if holds else {}
        source_ancestors = {source: directory_ancestors(source, chain, mounts)
                            for source, chain in source_chains.items()}
        scratch_chain = directory_chain(roots.scratch) if holds else {}
        for hold in holds:
            chain = directory_chain(hold.path)
            ancestors = directory_ancestors(hold.path, chain, mounts)
            held_folder = chain.get(hold.path)
            overlaps = {source for source, source_chain in source_chains.items()
                        if source_chain.get(source) in ancestors or held_folder in source_ancestors[source]}
            if overlaps or scratch_chain.get(roots.scratch) in ancestors:
                kept(hold.path, f"open scratch hold, owner {hold.owner}")
                blocked.update(overlaps)
                summary.refused_other += 1
        if summary.refused_other:
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
            sources.discard(pending.move.source)

    eligible = sources - blocked
    leaves = tuple(entry for entry in entries if isinstance(entry, ScratchpadLeaf) and entry.source in eligible)
    for entry in entries:
        if isinstance(entry, UnrecognizedEntry):
            kept(entry.path, entry.reason)
            summary.refused_other += int(entry.blocks_cutover)

    uses = process_uses(tuple(leaf.source for leaf in leaves), session_pids)
    destinations: set[Path] = set()
    for leaf in leaves:
        journaled = False
        try:
            if leaf.source.is_relative_to(roots.sessions):
                raise MoveRefused("source is inside the destination root")
            check_source_idle(leaf.source, names.registry, lambda: uses)
            name = names.destination(leaf.source.name)
            move = PlannedCopy(leaf.source, name.folder / leaf.relative, name.rule, name.session_id)
            size = tree_size(move.source, mounts)
            show_copy(move, size)
            check_destination(root_fd, roots.sessions, move.target, destinations)
            if not dry_run:
                check_copy_idle(leaf.source, session_pids)
                names.remember(name, root_fd)
                journaled = True
                size = start_copy(move, roots, root_fd, session_pids)
            destinations.add(move.target)
            summary.record_move(size)
        except LiveSource as error:
            summary.keep_live(leaf.source, error, journaled=journaled)
            if journaled:
                return
        except (MoveRefused, OSError) as error:
            kept(leaf.source, error)
            summary.refused_other += 1
            if not dry_run:
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

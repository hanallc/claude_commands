#!/usr/bin/env python3
# Gate rename chunk 6 deletes this folder and its configuration after deployment.
"""Convert stored gate step names during deployment.

Reverse converts every matching value to a name the old code can read.
This includes values that new code wrote before the forward deployment.
In both directions, refuse if both profile keys exist.

Set a natedev drain hold first.
Stop the admission socket, admission service, memory feed, and buildlog timer.
Wait for the buildlog service to stop.
Run this program through the checkout's scripts/lib/py -I.
Restart the stopped units after the conversion succeeds.
Release the hold last.
The program never stops services or changes a hold.
"""
from __future__ import annotations

import argparse
from collections.abc import Generator, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from typing import cast

from scripts.build_queue import memory_files, progress_profile
from scripts.build_targets import target_config
from scripts.buildlog import index, store
from scripts.lib import build_folders, conf, session_folder
from scripts.lib.atomic_file import sync_folder, write_text_atomic
from scripts.lib.process_start import Started, Unreadable, process_start
from scripts.lint import memory_admit
from scripts.mac_test import mac_queue_run
from scripts.production.gate_rename_deploy import move_gate_state


type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
RENAMES = {"merge-gate": "gate", "targeted-test": "test-affected"}
CLOUD_REQUEST_FILES = frozenset({"current.json", "request.json"})
MONTH = re.compile(r"\d{4}-\d{2}\.jsonl\Z")
SYSTEM_UNITS = ("build-admission.service", "build-admission.socket", "build-memory-feed.service")


class Direction(StrEnum):
    FORWARD = "forward"
    REVERSE = "reverse"

    def arguments(self) -> tuple[str, ...]:
        return ("--reverse",) if self is Direction.REVERSE else ()

    def names(self) -> Mapping[str, str]:
        return {new: old for old, new in RENAMES.items()} if self is Direction.REVERSE else RENAMES

    def gate_steps(self) -> tuple[str, str]:
        old, new = "merge-gate", RENAMES["merge-gate"]
        return (new, old) if self is Direction.REVERSE else (old, new)


class Refused(Exception):
    """A required deployment condition prevents conversion."""


@dataclass(frozen=True)
class RecordRoots:
    """The selected deployment records and assignment search folders."""

    buildlog: Path
    aws: Path
    review: Path
    wait_roots: tuple[Path, ...]
    cloud_roots: tuple[Path, ...] = ()
    direction: Direction = Direction.FORWARD


def configured_roots(direction: Direction) -> RecordRoots:
    return RecordRoots(store.root().expanduser().resolve(),
        target_config.ssh_target_state_directory("aws").resolve(),
        memory_files.configured_output_folder(conf.read("memory_feed")).resolve(), (), direction=direction)


@dataclass(frozen=True)
class RecordVersion:
    """The file identity that the conversion inspected."""

    device: int
    inode: int
    size: int
    modified_ns: int

    @classmethod
    def read(cls, path: Path) -> RecordVersion:
        value = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(value.st_mode):
            raise Refused(f"record is not a regular file: {path}")
        return cls(value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)


@dataclass(frozen=True)
class RecordChange:
    """A validated record has exact step values to convert."""

    path: Path
    fields: frozenset[str]
    names: Mapping[str, str]
    count: int
    version: RecordVersion


@dataclass(frozen=True)
class ProfileRename:
    """An existing full-gate profile has a known replacement key."""

    old: Path
    new: Path


def content_digest(path: Path) -> str | None:
    try:
        with move_gate_state.open_record(path) as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    except FileNotFoundError:
        return None


def write_snapshot(files: Sequence[Path], folder: Path | None) -> dict[str, object]:
    identity = move_gate_state.path_identity(folder, directory=True) if folder is not None else None
    return {
        "files": {str(path): content_digest(path) for path in files},
        "folder": str(folder) if folder is not None else None,
        "identity": list(identity) if identity is not None else None,
    }


@dataclass(frozen=True)
class ConversionWrites:
    """Keep evidence of the first change across interruptions."""

    marker: Path | None

    @property
    def pending(self) -> Path | None:
        return self.marker.with_suffix(".pending") if self.marker is not None else None

    def requires_completion(self) -> bool:
        """Clear a pending write only when its original state is visible."""
        marker, pending = self.marker, self.pending
        if marker is None or pending is None:
            return False
        if move_gate_state.path_identity(marker) is not None:
            return True
        try:
            saved = cast(object, json.loads(move_gate_state.read_record(pending)))
        except FileNotFoundError:
            return False
        except (OSError, ValueError):
            return True
        if not isinstance(saved, dict):
            return True
        fields = cast(dict[str, object], saved)
        files, folder = fields.get("files"), fields.get("folder")
        if fields.get("unresolved") is True:
            return True
        if not isinstance(files, dict) or not files or (folder is not None and not isinstance(folder, str)):
            return True
        paths = tuple(Path(name) for name in cast(dict[str, object], files))
        source = Path(folder) if folder is not None else None
        if any(not path.is_absolute() for path in paths) or (source is not None and not source.is_absolute()):
            return True
        try:
            if write_snapshot(paths, source) != fields:
                return True
        except (OSError, ValueError):
            return True
        pending.unlink()
        sync_folder(pending.parent)
        return False

    def clear(self) -> None:
        if self.marker is not None:
            for path in (self.marker, self.pending):
                if path is not None:
                    path.unlink(missing_ok=True)
            sync_folder(self.marker.parent)

    @contextmanager
    def publish(self, *files: Path, folder: Path | None = None) -> Generator[None]:
        marker, pending = self.marker, self.pending
        if marker is None or pending is None or move_gate_state.path_identity(marker) is not None:
            yield
            return
        unresolved = self.requires_completion()
        original = write_snapshot(files, folder)
        if unresolved:
            # This proof cannot account for an earlier write with an unknown result.
            original["unresolved"] = True
        write_text_atomic(pending, json.dumps(original) + "\n", new_file_mode=0o600)
        yield
        os.replace(pending, marker)
        sync_folder(marker.parent)


def report(message: str, started: float, count: int = 0) -> None:
    elapsed = time.monotonic() - started
    print(f"{datetime.now(UTC).isoformat(timespec='milliseconds')} {message}; seconds={elapsed:.6f}; changed={count}", flush=True)


def require_quiet_services() -> None:
    for user, unit in [(False, name) for name in SYSTEM_UNITS] + [(True, "buildlog.service")]:
        command = ["systemctl", *(["--user"] if user else []), "show", "--property=ActiveState", "--value", unit]
        answer = subprocess.run(command, capture_output=True, text=True, check=False)
        state = answer.stdout.strip()
        if answer.returncode or state not in {"inactive", "failed"}:
            detail = state or answer.stderr.strip() or f"exit {answer.returncode}"
            raise Refused(f"{unit} must be stopped: {detail}")


def recorded_callers(value: JsonValue) -> Iterator[tuple[int, str]]:
    """Read local caller identities from current and legacy queue records."""
    if isinstance(value, dict):
        host = value.get("host")
        if isinstance(host, str) and host != store.host_name():
            return
        pid = value.get("pid")
        start = value.get("start_time", value.get("proc_start"))
        if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 and isinstance(start, str) and start:
            yield pid, start
        for item in value.values():
            yield from recorded_callers(item)
    elif isinstance(value, list):
        for item in value:
            yield from recorded_callers(item)


def require_no_waiting_callers(roots: RecordRoots) -> None:
    """Refuse live callers that can retain a request across the deployment."""
    identities: set[tuple[int, str]] = set()
    admission = roots.buildlog / "admission"
    paths = [admission / "state.json", *sorted(admission.glob("*.waiting")),
             *sorted(admission.glob("*.reservation"))]
    for path in paths:
        if os.path.lexists(path):
            identities.update(recorded_callers(decode_record(path, move_gate_state.read_record(path))))
    entries = {"queue_place.py", "admission_client.py", "memory_admit.py", "gate.py", "merge_gate.py", "targeted_test.py"}
    modules = {"scripts.build_queue.queue_place", "scripts.build_queue.admission_client",
               "scripts.lint.memory_admit", "scripts.production.gate", "scripts.production.merge_gate",
               "scripts.production.targeted_test"}
    blocked: list[str] = []
    for directory in sorted(move_gate_state.PROC_ROOT.iterdir()):
        if not directory.name.isdecimal():
            continue
        pid = int(directory.name)
        if pid == os.getpid():
            continue
        try:
            if directory.stat().st_uid != os.getuid():
                continue
            start = process_start(pid)
            if isinstance(start, Unreadable):
                raise Refused(f"cannot read caller identity: pid={pid}: {start.reason}")
            if not isinstance(start, Started):
                continue
            words = (directory / "cmdline").read_bytes().decode("utf-8", errors="replace").split("\0")
            try:
                environment = (directory / "environ").read_bytes().split(b"\0")
            except PermissionError:
                environment = []
            for entry in environment:
                key, separator, value = entry.partition(b"=")
                if separator and key == b"BUILDLOG_HOLD_WAITER_FILE" and value:
                    marker = Path(os.fsdecode(value))
                    try:
                        identities.update(recorded_callers(decode_record(marker, move_gate_state.read_record(marker))))
                    except FileNotFoundError:
                        pass
            # Inspect argument words, never shell source or Python source strings.
            named = any(Path(word).name in entries or word in modules for word in words if word)
            hold = "wait-local" in words and any(Path(word).name == "build_targets.py" for word in words if word)
            if ((pid, start.start) in identities or named or hold) and process_start(pid) == start:
                command = " ".join(word for word in words if word) or "[empty command]"
                blocked.append(f"pid={pid} start={start.start} command={command}")
        except (FileNotFoundError, ProcessLookupError):
            continue
    if blocked:
        raise Refused("live hold or queue callers must exit before conversion: " + "; ".join(blocked))


def require_drained(roots: RecordRoots) -> None:
    move_gate_state.require_hold()
    require_quiet_services()
    require_no_waiting_callers(roots)
    admission = roots.buildlog / "admission"
    if admission.exists():
        holders = [holder for holder in move_gate_state.state_holders(admission) if holder.pid != os.getpid()]
        if holders:
            detail = "; ".join(f"pid={holder.pid} command={holder.command}" for holder in holders)
            raise Refused(f"a process holds admission records: {detail}")


def convert(value: JsonValue, fields: frozenset[str], names: Mapping[str, str]) -> int:
    """Convert only exact values of named schema fields."""
    count = 0
    if isinstance(value, dict):
        for key, item in value.items():
            if key in fields and isinstance(item, str) and item in names:
                value[key] = names[item]
                count += 1
            else:
                count += convert(item, fields, names)
    elif isinstance(value, list):
        for item in value:
            count += convert(item, fields, names)
    return count


def record_text(path: Path, data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"Record must be UTF-8: {path}") from error


def decode_record(path: Path, data: bytes) -> JsonValue:
    try:
        return cast(JsonValue, json.loads(record_text(path, data)))
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid JSON record {path}: {error}") from error


def records(path: Path) -> Iterator[tuple[bytes, JsonValue]]:
    with move_gate_state.open_record(path) as stream:
        if path.suffix == ".jsonl":
            for line in stream:
                if line.strip():
                    yield line, decode_record(path, line)
                else:
                    yield line, None
        else:
            if path.suffix == ".gz":
                with gzip.GzipFile(fileobj=stream) as compressed:
                    data = compressed.read()
            else:
                data = stream.read()
            yield data, decode_record(path, data)


def wait_files(roots: RecordRoots) -> list[Path]:
    found: set[Path] = set()
    skipped = {".git", "node_modules", "__pycache__", "target", "typecheck-venv"}
    for root in roots.wait_roots:
        started = time.monotonic()
        if not root.exists():
            report(f"Assignment search absent: {root}", started)
            continue
        def on_error(error: OSError) -> None:
            raise Refused(f"cannot search assignments: {error}") from error
        for directory, names, files in os.walk(root, followlinks=False, onerror=on_error):
            parent = Path(directory)
            names[:] = sorted(name for name in names if name not in skipped
                              and not (parent / name).is_symlink()
                              and (parent / name).stat().st_uid == os.getuid())
            for name in files:
                if name.endswith(".wait"):
                    path = parent / name
                    if path.lstat().st_uid != os.getuid():
                        continue
                    text = record_text(path, move_gate_state.read_record(path))
                    try:
                        value = cast(JsonValue, json.loads(text))
                    except json.JSONDecodeError:
                        if path.with_suffix("").exists():
                            raise Refused(f"cannot decode an assignment wait record: {path}") from None
                        continue
                    if isinstance(value, dict) and "last_reply" in value:
                        found.add(path)
        report(f"Assignment search complete: {root}; found={len(found)}", started)
    return sorted(found)


def record_paths(roots: RecordRoots) -> dict[Path, frozenset[str]]:
    paths: dict[Path, frozenset[str]] = {}
    steps = frozenset({"step", "program"})
    admission = roots.buildlog / "admission"
    for name in ("state.json", "anon_peaks.jsonl", "queue_times.jsonl", "queue.json"):
        if os.path.lexists(admission / name):
            paths[admission / name] = steps
    for pattern in ("*.reservation", "*.waiting"):
        paths.update((path, steps) for path in admission.glob(pattern))
    paths.update((path, steps) for path in (roots.buildlog / "pending-steps").glob("*.json"))
    for directory in roots.buildlog.iterdir():
        if directory.is_dir() and not directory.is_symlink() and directory.name not in ("admission", "pending-steps"):
            paths.update((path, frozenset({"step"})) for path in directory.iterdir() if MONTH.fullmatch(path.name))
    mode_fields = frozenset({"mode"})
    # Both cloud writers use the same StepJob values.
    for name in ("cloud-gate-launches.jsonl", "cost-ledger.json"):
        if os.path.lexists(roots.aws / name):
            paths[roots.aws / name] = mode_fields
    for directory in roots.buildlog.iterdir():
        if directory.is_dir() and not directory.is_symlink():
            paths.update((path, frozenset({"step"})) for path in directory.glob("results/????-??/*.json.gz"))
    for root in (*roots.cloud_roots, roots.aws / "paired-trials"):
        if not root.exists():
            continue
        for directory, names, files in os.walk(root, followlinks=False):
            names[:] = sorted(name for name in names if not (Path(directory) / name).is_symlink())
            for name in files:
                if name in CLOUD_REQUEST_FILES:
                    paths[Path(directory) / name] = mode_fields
                elif name in {"remote-result.json", "outcome.json"}:
                    paths[Path(directory) / name] = frozenset({"mode", "step"})
    paths.update((path, steps) for path in wait_files(roots))
    return paths


def inspect_records(roots: RecordRoots, names: Mapping[str, str]) -> tuple[list[RecordChange], set[str]]:
    changes: list[RecordChange] = []
    hosts = {store.host_name(), *(path.name for path in roots.buildlog.iterdir() if path.is_dir())}
    wire_modes = Direction.REVERSE.names()
    for path, fields in sorted(record_paths(roots).items()):
        started = time.monotonic()
        version = RecordVersion.read(path)
        # Both controllers write legacy modes to cloud request files.
        record_names = wire_modes if path.name in CLOUD_REQUEST_FILES else names
        count = 0
        for _raw, value in records(path):
            if isinstance(value, dict) and isinstance(value.get("host"), str):
                hosts.add(cast(str, value["host"]))
            count += convert(value, fields, record_names)
        if RecordVersion.read(path) != version:
            raise Refused(f"record changed during inspection: {path}")
        if count:
            changes.append(RecordChange(path, fields, record_names, count, version))
        report(f"Inspected {path}", started, count)
    return changes, hosts


def profile_renames(roots: RecordRoots, hosts: set[str]) -> list[ProfileRename]:
    parent = roots.buildlog / "admission/progress_profiles"
    renames: list[ProfileRename] = []
    old_step, new_step = roots.direction.gate_steps()
    if hasattr(memory_admit, "GATE_TESTS"):
        shapes = (memory_admit.GATE_TESTS, memory_admit.GATE_TYPES)
    else:
        # The old code uses these names for the same shapes.
        shapes = (cast(str, getattr(memory_admit, "MERGE_GATE_TESTS")),
                  cast(str, getattr(memory_admit, "MERGE_GATE_TYPES")))
    for host in sorted(hosts):
        for shape in shapes:
            old = parent / progress_profile.profile_key((host, old_step, shape))
            new = parent / progress_profile.profile_key((host, new_step, shape))
            if not os.path.lexists(old):
                continue
            if old.is_symlink() or not old.is_dir():
                raise Refused(f"profile must be a directory: {old}")
            if os.path.lexists(new):
                raise Refused(f"both profile keys exist: {old} and {new}")
            renames.append(ProfileRename(old, new))
    return renames


def index_tables(connection: sqlite3.Connection) -> set[str]:
    return {row[0] for row in cast(list[tuple[str]], connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall())}


def inspect_indexes(roots: RecordRoots) -> list[Path]:
    paths = sorted(roots.buildlog.glob("index-*.sqlite"))
    for path in paths:
        _ = RecordVersion.read(path)
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
            tables = index_tables(connection)
            for table in ("gate_runs", "steps"):
                if table in tables:
                    _ = connection.execute(f"SELECT step FROM {table} LIMIT 0")
    return paths


def write_record(change: RecordChange, writes: ConversionWrites) -> None:
    started = time.monotonic()
    if RecordVersion.read(change.path) != change.version:
        raise Refused(f"record changed before conversion: {change.path}")
    mode = stat.S_IMODE(change.path.stat().st_mode)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{change.path.name}.rename-", dir=change.path.parent)
    temp = Path(temporary)
    try:
        count = 0
        with os.fdopen(descriptor, "wb") as output:
            for raw, value in records(change.path):
                changed = convert(value, change.fields, change.names)
                count += changed
                data = (json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode() + b"\n" if changed else raw)
                _ = output.write(gzip.compress(data, mtime=0) if change.path.suffix == ".gz" else data)
            output.flush()
            os.fsync(output.fileno())
        if count != change.count or RecordVersion.read(change.path) != change.version:
            raise Refused(f"record changed during conversion: {change.path}")
        temp.chmod(mode)
        with writes.publish(change.path):
            os.replace(temp, change.path)
        sync_folder(change.path.parent)
        report(f"Converted {change.path}", started, count)
    finally:
        temp.unlink(missing_ok=True)


def update_index(path: Path, names: Mapping[str, str], writes: ConversionWrites) -> int:
    started = time.monotonic()
    count = 0
    updates: list[tuple[str, str, str]] = []
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as reader:
        tables = index_tables(reader)
        for table in ("gate_runs", "steps"):
            if table not in tables:
                continue
            for old, new in names.items():
                if reader.execute(f"SELECT 1 FROM {table} WHERE step = ? LIMIT 1", (old,)).fetchone():
                    updates.append((table, old, new))
    if updates:
        # A commit can change the WAL while the main database stays unchanged.
        sidecars = (Path(str(path) + suffix) for suffix in index.INDEX_SIDECAR_SUFFIXES if suffix != "-shm")
        with writes.publish(path, *sidecars), closing(sqlite3.connect(path)) as connection:
            _ = connection.execute("BEGIN IMMEDIATE")
            for table, old, new in updates:
                cursor = connection.execute(f"UPDATE {table} SET step = ? WHERE step = ?", (new, old))
                count += cursor.rowcount
            if not count:
                raise Refused(f"index changed before conversion: {path}")
            connection.commit()
    report(f"Converted index {path}", started, count)
    return count


@dataclass(frozen=True)
class Conversion:
    """The inspected writes that the conversion applies."""

    changes: list[RecordChange]
    renames: list[ProfileRename]
    indexes: list[Path]
    caches: list[Path]


def check_early(roots: RecordRoots) -> None:
    """Check the conditions that live services cannot change before they stop."""
    move_gate_state.require_hold()
    require_no_waiting_callers(roots)
    require_expired_uploaded_attempts(roots)
    for path in record_paths(roots):
        try:
            for _record in records(path):
                pass
        except FileNotFoundError:
            # Active services can remove temporary records before the full check.
            continue


def inspect_conversion(roots: RecordRoots, started: float) -> Conversion:
    """Check every refusal condition and read every record without a write."""
    require_drained(roots)
    report("Deployment conditions passed", started)
    changes, hosts = inspect_records(roots, roots.direction.names())
    renames = profile_renames(roots, hosts)
    indexes = inspect_indexes(roots)
    require_expired_uploaded_attempts(roots)
    caches = [roots.review / name for name in ("memory.json", "report.json") if (roots.review / name).exists()]
    for path in caches:
        _ = RecordVersion.read(path)
    require_drained(roots)
    report(f"Inspection complete; files={len(changes)}; profile folders={len(renames)}", started)
    return Conversion(changes, renames, indexes, caches)


def migrate(roots: RecordRoots, writes: ConversionWrites) -> int:
    started = time.monotonic()
    conversion = inspect_conversion(roots, started)
    changes, renames, indexes, caches = conversion.changes, conversion.renames, conversion.indexes, conversion.caches
    count = 0
    names = roots.direction.names()
    for change in changes:
        write_record(change, writes)
        count += change.count
    for rename in renames:
        began = time.monotonic()
        if os.path.lexists(rename.new):
            raise Refused(f"replacement profile appeared: {rename.new}")
        if move_gate_state.path_identity(rename.old) is None:
            raise Refused(f"source profile disappeared: {rename.old}")
        with writes.publish(rename.new, folder=rename.old):
            move_gate_state.rename_folder(rename.old, rename.new)
        report(f"Renamed profile {rename.old} -> {rename.new}", began, 1)
        count += 1
    for path in indexes:
        count += update_index(path, names, writes)
    for path in caches:
        began = time.monotonic()
        with writes.publish(path):
            path.unlink()
        sync_folder(path.parent)
        report(f"Removed derived cache {path}", began, 1)
        count += 1
    report("Affected-test profile folders stay unchanged; the existing profile sweep removes stale or invalid records", started)
    report("Conversion complete", started, count)
    return 0


def discover_cloud_roots(checkouts: Sequence[Path]) -> tuple[Path, ...]:
    """Find the Git attempt folders for each registered live checkout."""
    candidates: set[Path] = set()
    for checkout in checkouts:
        if not checkout.exists():
            continue
        answer = subprocess.run(["git", "-C", str(checkout), "worktree", "list", "--porcelain", "-z"],
                                capture_output=True, text=True, check=False)
        if answer.returncode:
            raise Refused(f"cannot list live checkouts for {checkout}: {answer.stderr.strip()}")
        candidates.update(Path(part.removeprefix("worktree ")) for part in answer.stdout.split("\0")
                          if part.startswith("worktree "))
    found: set[Path] = set()
    for checkout in sorted(candidates):
        if not checkout.exists():
            continue
        answer = subprocess.run(["git", "-C", str(checkout), "rev-parse", "--path-format=absolute", "--git-path", "cloud-gates"],
                                capture_output=True, text=True, check=False)
        if answer.returncode:
            raise Refused(f"cannot find cloud attempt records for {checkout}: {answer.stderr.strip()}")
        path = Path(answer.stdout.strip()).resolve()
        report(f"Cloud attempt search: checkout={checkout}; folder={path}; exists={path.exists()}", time.monotonic())
        found.add(path)
    return tuple(sorted(found))


def require_expired_uploaded_attempts(roots: RecordRoots) -> None:
    """Refuse an old uploaded payload that a running attempt can still read."""
    now = time.time()
    modes = set(RENAMES)
    if roots.direction is Direction.REVERSE:
        # The new controller can send the old wire names to new code.
        modes.update(RENAMES.values())
    for root in (*roots.cloud_roots, roots.aws / "paired-trials"):
        if not root.exists():
            continue
        for request in root.rglob("request.json"):
            data = cast(dict[str, JsonValue], decode_record(request, move_gate_state.read_record(request)))
            mode = data.get("mode")
            if not isinstance(mode, str) or mode not in modes or not (request.parent / "launch.json").exists():
                continue
            outcome = request.parent / "outcome.json"
            saved = (cast(dict[str, JsonValue], decode_record(outcome, move_gate_state.read_record(outcome)))
                     if os.path.lexists(outcome) else {})
            expires = data.get("expires", 0)
            status = saved.get("status")
            if status not in ("complete", "fallback", "interrupted") and isinstance(expires, (int, float)) and expires > now:
                raise Refused(f"an old uploaded attempt can still read cloud objects; let it finish or expire: {request.parent}")


def default_wait_roots() -> tuple[Path, ...]:
    """Find the tool folders that hold placement assignments."""
    settings = conf.read_or_repository("build_folders")
    folders = (session_folder.configured_root(), Path(settings.text("roots.system")),
               Path(settings.text("roots.gate")), Path(settings.text("roots.scratch")),
               build_folders.inherited_temporary_parent())
    parents = tuple(dict.fromkeys(folder.expanduser().resolve() for folder in folders))
    found = {Path.home() / mac_queue_run.RECEIPTS}
    tool = re.compile(r"(?:verify|copy-watch)(?:-\d+)?\Z|verify-hold\..+")
    skipped = {".git", "node_modules", "__pycache__", "target", "typecheck-venv"}
    for parent in parents:
        if not parent.exists():
            continue
        def on_error(error: OSError) -> None:
            if isinstance(error, FileNotFoundError):
                return
            if isinstance(error, PermissionError):
                filename = cast(str | None, error.filename)
                report(f"Assignment discovery skipped an unreadable folder: {filename}", time.monotonic())
                return
            raise Refused(f"cannot discover assignment folders: {error}") from error
        for directory, names, _files in os.walk(parent, followlinks=False, onerror=on_error):
            path = Path(directory)
            if tool.fullmatch(path.name):
                found.add(path)
                names.clear()
                continue
            kept: list[str] = []
            for name in sorted(names):
                child = path / name
                try:
                    if name not in skipped and not child.is_symlink() and child.stat().st_uid == os.getuid():
                        kept.append(name)
                except FileNotFoundError:
                    continue
            names[:] = kept
        report(f"Assignment folder discovery complete: {parent}", time.monotonic())
    return tuple(sorted(found))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--buildlog", type=Path)
    _ = parser.add_argument("--aws-state", type=Path)
    _ = parser.add_argument("--review-page", type=Path)
    _ = parser.add_argument("--reverse", action="store_true")
    _ = parser.add_argument("--write-marker", type=Path, help=argparse.SUPPRESS)
    _ = parser.add_argument("--wait-root", type=Path, action="append", default=None,
                            help="search this assignment folder; repeat to replace the default search folders")
    _ = parser.add_argument("--cloud-root", type=Path, action="append", default=None,
                            help="use this cloud attempt folder; repeat to replace checkout discovery")
    _ = parser.add_argument("--checkout", type=Path, action="append", default=None,
                            help="include the registered worktrees of this repository in cloud attempt discovery")
    _ = parser.add_argument("--check", choices=("early", "full"), default=None,
                            help="check the refusal conditions and write nothing")
    args = parser.parse_args(argv)
    try:
        direction = Direction.REVERSE if cast(bool, args.reverse) else Direction.FORWARD
        defaults = configured_roots(direction)
        requested = cast(list[Path] | None, args.wait_root)
        wait_roots = (tuple(path.expanduser().resolve() for path in requested) if requested is not None
                      else default_wait_roots())
        requested_cloud = cast(list[Path] | None, args.cloud_root)
        checkouts = cast(list[Path] | None, args.checkout)
        cloud_roots = (tuple(path.expanduser().resolve() for path in requested_cloud) if requested_cloud is not None else
                       discover_cloud_roots(checkouts if checkouts is not None else (Path.home() / ".claude",)))
        roots = RecordRoots((cast(Path | None, args.buildlog) or defaults.buildlog).expanduser().resolve(),
            (cast(Path | None, args.aws_state) or defaults.aws).expanduser().resolve(),
            (cast(Path | None, args.review_page) or defaults.review).expanduser().resolve(),
            wait_roots, cloud_roots, direction)
        check = cast(str | None, args.check)
        if check is None:
            return migrate(roots, ConversionWrites(cast(Path | None, args.write_marker)))
        started = time.monotonic()
        if check == "early":
            check_early(roots)
        else:
            _ = inspect_conversion(roots, started)
        report(f"Check passed: {check}", started)
        return 0
    except (Refused, OSError, EOFError, ValueError, sqlite3.Error) as error:
        report(f"Refused: {error}", time.monotonic())
        return 2


if __name__ == "__main__":
    sys.exit(main())

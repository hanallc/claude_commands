"""Keep only the staff row IDs that still need a start."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import fcntl
import json
import os
import sys
import time
from collections.abc import Collection, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from threading import Lock
from typing import IO, cast


from scripts.lib import conf, lock_holder
from scripts.lib.atomic_file import write_text_atomic
from scripts.production import staff, session_staff
from scripts.production.production_names import LIVE_DOCUMENT_SUFFIX
from scripts.agents import worker_lifecycle
from scripts.agents.stop_results import shutdown_state_root


def row_folder(row: staff.StaffRow, record: staff.StaffSnapshot, *,
               resolver: session_staff.RowResolver | None = None) -> tuple[str, Path]:
    if isinstance(row.launch_folder, staff.SessionLaunchFolder):
        return row.launch_folder.machine, row.launch_folder.path
    facts = session_staff.RowResolver(record) if resolver is None else resolver
    # A recorded worker folder remains valid for older stopped worker rows.
    tree = row.worktree if isinstance(row.worktree, (staff.PresentWorktree, staff.RemovedWorktree)) else facts.unit_worktree(row)
    if isinstance(tree, (staff.PresentWorktree, staff.RemovedWorktree)):
        return tree.machine, tree.path
    if row.kind == "unit":
        raise ValueError(f"{row.name}: the staff worktree is unavailable")
    if isinstance(row.doc, (staff.LiveDoc, staff.WrappedDoc)):
        if row.kind == "showrunner" and isinstance(row.doc, staff.LiveDoc):
            document = facts.production_doc(row)
            if isinstance(document, session_staff.ProductionDoc):
                return document.machine, session_staff.enrolled_launch_directory(
                    document.text.splitlines(), document.path.parent)
            if row.doc.path.name.endswith(LIVE_DOCUMENT_SUFFIX):
                raise ValueError(f"{row.name}: {document.reason}")
        return row.doc.machine, row.doc.path.parent
    if row.kind == "producer":
        from scripts.agents import remote

        return remote.local_machine(), Path.home()
    document = facts.production_doc(row)
    if isinstance(document, session_staff.ProductionDoc):
        return document.machine, session_staff.enrolled_launch_directory(document.text.splitlines(), document.path.parent)
    raise ValueError(f"{row.name}: {document.reason}")


def record_path() -> Path:
    return shutdown_state_root() / "restart.json"


def _activity_path() -> Path:
    return record_path().with_suffix(".active")


def _holder_folder() -> Path:
    return record_path().with_suffix(".holders")


_RECORD_LOCK_SECONDS: ContextVar[float | None] = ContextVar("_RECORD_LOCK_SECONDS", default=None)


class RecordBusy(TimeoutError):
    """The restart record lock did not become available before the deadline."""


def record_lock_seconds() -> float:
    key = "restart_record_lock_seconds"
    config = conf.read_or_repository("shutdown")
    if isinstance(config.settings, dict) and config.effective_key(key) not in config.settings:
        config = conf.read(Path(__file__).resolve().parents[2] / "config" / "shutdown.conf")
    return config.number(key)


def _gap_path(pid: int, row_id: str) -> Path:
    return _holder_folder() / "claims" / f"{pid}-{sha256(row_id.encode()).hexdigest()}"


@dataclass
class RestartActivity:
    holder_path: Path
    _recorded: bool = field(default=False, init=False)
    _reported: bool = field(default=False, init=False)
    _guard: Lock = field(default_factory=Lock, init=False)
    _claims: dict[str, IO[str]] = field(default_factory=dict, init=False)

    def _anomaly(self, error: OSError | ValueError) -> None:
        if not self._reported:
            self._reported = True
            detail = str(error).partition("\n")[0] or type(error).__name__
            print(f"restart: holder record anomaly: {detail}", file=sys.stderr)

    def publish(self) -> None:
        try:
            self.holder_path.parent.mkdir(exist_ok=True)
            write_text_atomic(self.holder_path, json.dumps(lock_holder.own_fields()), new_file_mode=0o600)
            self._recorded = True
        except (OSError, ValueError) as error:
            self._anomaly(error)

    def claim_rows(self, rows: Collection[str]) -> None:
        """Hold the rows between their confirmed stop and their start."""
        if not self._recorded:
            return
        with self._guard:
            for row_id in rows:
                if row_id in self._claims:
                    continue
                path = _gap_path(os.getpid(), row_id)
                try:
                    path.parent.mkdir(exist_ok=True)
                    stream = path.open("a")
                    try:
                        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except OSError:
                        stream.close()
                        raise
                    self._claims[row_id] = stream
                except (OSError, ValueError) as error:
                    self._anomaly(error)

    def release_rows(self, rows: Collection[str]) -> None:
        with self._guard:
            for row_id in rows:
                stream = self._claims.pop(row_id, None)
                if stream is None:
                    continue
                try:
                    stream.close()
                    _gap_path(os.getpid(), row_id).unlink(missing_ok=True)
                except (OSError, ValueError) as error:
                    self._anomaly(error)

    def close(self) -> None:
        self.release_rows(tuple(self._claims))
        try:
            self.holder_path.unlink(missing_ok=True)
        except (OSError, ValueError) as error:
            self._anomaly(error)


@contextmanager
def active_operation(*, record_lock_seconds: float | None = None) -> Generator[RestartActivity]:
    path = _activity_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        activity = RestartActivity(_holder_folder() / str(os.getpid()))
        with lock_holder.restart_operation(), _record_wait(record_lock_seconds):
            activity.publish()
            try:
                yield activity
            finally:
                activity.close()


@contextmanager
def _record_wait(seconds: float | None) -> Generator[None]:
    token = _RECORD_LOCK_SECONDS.set(seconds)
    try:
        yield
    finally:
        _RECORD_LOCK_SECONDS.reset(token)


def _read_holder(path: Path) -> lock_holder.HolderRecord | None:
    try:
        with path.open("rb") as stream:
            holder = lock_holder.read_record(stream)
    except OSError:
        return None
    if holder is not None and holder.restart_scope and holder.start and path.name == str(holder.pid):
        return holder
    return None


def has_holder_record(record: lock_holder.HolderRecord) -> bool:
    saved = _read_holder(_holder_folder() / str(record.pid))
    return saved is not None and saved.start == record.start and saved.restart_scope == record.restart_scope


def restart_holders(row_id: str) -> list[lock_holder.HolderRecord]:
    return [holder for path in sorted(_holder_folder().glob("[0-9]*"))
            if (holder := _read_holder(path)) is not None and _lock_is_held(_gap_path(holder.pid, row_id))]


def prove_restart_holder(row_id: str) -> lock_holder.ProvedLockHolder | None:
    """Prove the restart's active claim on this row."""
    holders = [proof for holder in restart_holders(row_id)
               if (proof := lock_holder.prove(_gap_path(holder.pid, row_id), [holder])) is not None]
    return holders[0] if len(holders) == 1 else None


def forget_moved(row_id: str) -> None:
    """Remove a confirmed move from recovery while the caller holds its row lock."""
    if row_id in read():
        with _record_wait(record_lock_seconds()):
            remove_started([row_id])


def _lock_is_held(path: Path) -> bool:
    try:
        with path.open("r") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
    except FileNotFoundError:
        pass
    return False


def active() -> bool:
    return _lock_is_held(_activity_path())


def read() -> list[str]:
    try:
        value = cast(object, json.loads(record_path().read_text(encoding="utf-8")))
    except FileNotFoundError:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in cast(list[object], value)):
        raise ValueError("restart record must contain only staff row IDs")
    return list(dict.fromkeys(cast(list[str], value)))


def save(row_ids: list[str]) -> None:
    path = record_path()
    content = json.dumps(list(dict.fromkeys(row_ids))) + "\n"
    if not path.exists() or path.read_text(encoding="utf-8") != content:
        write_text_atomic(path, content)


def migrate(*, stop_after_first: bool = False) -> tuple[str, ...]:
    """Replace legacy names with fixed row ids under the record lock."""
    path = record_path().with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        references = read()
        record = staff.snapshot()
        rows: list[str] = []
        proven: set[staff.StaffId] = set()
        for reference in references:
            row = record.by_member_reference(reference) or record.by_path(reference)
            if row is None:
                matches = [item for item in record.rows if item.name == reference]
                if len(matches) != 1:
                    print(f"{' '.join(reference.split())}: migration anomaly: staff ownership is unavailable", file=sys.stderr)
                    rows.append(reference)
                    continue
                row = matches[0]
            rows.append(str(row.id))
            proven.add(row.id)
        if rows == references:
            return ()
        # Resolve ownership again before publication, including a concurrent clear.
        current = staff.snapshot()
        for row_id in proven:
            if current.by_id(row_id) != record.by_id(row_id):
                raise ValueError(f"{row_id}: staff ownership changed; rerun the migration")
        save(rows)
        return (str(record_path()) + ("; stopped after first file" if stop_after_first else ""),)


def update(*, started: list[str], pending: list[str]) -> None:
    """Apply this run's changes without replacing another run's pending rows."""
    path = record_path().with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        wait = _RECORD_LOCK_SECONDS.get()
        if wait is None:
            fcntl.flock(lock, fcntl.LOCK_EX)
        else:
            try:
                worker_lifecycle.acquire_flock(lock, deadline=time.monotonic() + wait)
            except worker_lifecycle.OperationDeadlineExpired as error:
                action = (f"remove {', '.join(started)} from" if started else
                          f"save {', '.join(pending)} to" if pending else "update")
                raise RecordBusy(f"could not {action} the restart record: the record is busy") from error
        save([row for row in read() if row not in started] + pending)


def remove_started(row_ids: list[str]) -> None:
    """Remove confirmed starts while the caller holds their staff row locks."""
    update(started=row_ids, pending=[])


def forget(row_ids: list[str]) -> None:
    """Remove ids that will not restart: a removed staff row or a suspended worker."""
    # Most removals and closes name no saved id; those read the record and write nothing.
    if set(row_ids) & set(read()):
        update(started=row_ids, pending=[])


class MigrationArguments(argparse.Namespace):
    stop_after_first: bool = False


if __name__ == "__main__":

    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("command", choices=("migrate",))
    _ = parser.add_argument("--stop-after-first", action="store_true")
    options = parser.parse_args(namespace=MigrationArguments())
    changes = migrate(stop_after_first=options.stop_after_first)
    print("\n".join(changes) if changes else "unchanged")

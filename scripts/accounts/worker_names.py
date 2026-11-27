"""Convert earlier account-role and journal names at their storage boundary."""
from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Generator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO, cast

from scripts.lib import atomic_file


@dataclass(frozen=True)
class LegacyAccountsState:
    file: Path
    lock: Path


@dataclass(frozen=True)
class MigratedAccountsState:
    file: Path
    lock: Path


type AccountsState = LegacyAccountsState | MigratedAccountsState


def legacy_accounts_state(home: Path) -> LegacyAccountsState:
    file = home / ".claude/config/accounts.json"
    return LegacyAccountsState(file, file.with_suffix(".lock"))


def migrated_accounts_state(home: Path) -> MigratedAccountsState:
    file = home / ".local/state/accounts/accounts.json"
    return MigratedAccountsState(file, file.with_suffix(".lock"))


def accounts_state() -> AccountsState:
    home = Path.home()
    migrated = migrated_accounts_state(home)
    return migrated if migrated.file.exists() else legacy_accounts_state(home)


def accounts_config_file() -> Path:
    return accounts_state().file


def read_accounts_document() -> dict[str, object]:
    """Read the current document and retry if migration moved it."""
    state = accounts_state()
    try:
        text = state.file.read_text(encoding="utf-8")
    except FileNotFoundError:
        current = accounts_state()
        if current == state:
            raise
        text = current.file.read_text(encoding="utf-8")
        state = current
    value = cast(object, json.loads(text))
    if not isinstance(value, dict):
        raise ValueError(f"{state.file} must contain an object")
    return cast(dict[str, object], value)


@contextmanager
def roles_lock() -> Generator[AccountsState]:
    """Lock the current account state before any journal lock."""
    while True:
        state = accounts_state()
        path = state.lock
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                current = accounts_state()
                if current == state:
                    yield state
                    return
                if isinstance(state, LegacyAccountsState) and isinstance(current, MigratedAccountsState):
                    # A late opener may have created the old lock after the move.
                    path.unlink(missing_ok=True)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def migrate_accounts_state() -> MigratedAccountsState:
    """Publish the local account document at the new state path."""
    home = Path.home()
    migrated = migrated_accounts_state(home)
    legacy = legacy_accounts_state(home)
    with roles_lock() as state:
        if isinstance(state, LegacyAccountsState):
            migrated.file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if state.file.exists():
                _ = state.file.rename(migrated.file)
            else:
                with atomic_file.prepared_replacement(migrated.file, "{}\n", mode=0o600) as temporary:
                    _ = temporary.replace(migrated.file)
        elif legacy.file.exists():
            raise OSError(f"Both account state files exist: {legacy.file} and {migrated.file}")
        atomic_file.sync_folder(migrated.file.parent)
        legacy.lock.unlink(missing_ok=True)
        if legacy.lock.parent.is_dir():
            atomic_file.sync_folder(legacy.lock.parent)
    return migrated


def configuration(values: dict[str, object]) -> dict[str, object]:
    """Keep an explicitly selected worker account from older configurations."""
    result = dict(values)
    if "seats" in result:
        if "workers" not in result:
            result["workers"] = result["seats"]
        del result["seats"]
    return result


def journal_entry(value: object) -> object:
    """Return only worker names while retaining all other recorded evidence."""
    if not isinstance(value, dict):
        return value
    result = dict(cast(dict[str, object], value))
    for field in ("role", "subject"):
        if result.get(field) == "seats":
            result[field] = "workers"
    if result.get("kind") == "confirmed" and "seat" in result:
        if "worker" not in result:
            result["worker"] = result["seat"]
        del result["seat"]
    return result


def migrate_locked_journal(source: TextIO, path: Path) -> list[str]:
    """Keep a recovery image until conversion finishes, retaining the locked inode."""
    recovery = path.with_name(f".{path.name}.workers")
    _ = source.seek(0)
    lines = recovery.read_text(encoding="utf-8").splitlines(keepends=True) if recovery.exists() else source.readlines()
    converted: list[str] = []
    changed = False
    for line in lines:
        try:
            previous = cast(object, json.loads(line))
        except ValueError:
            converted.append(line)
            continue
        current = journal_entry(previous)
        if current != previous:
            converted.append(json.dumps(current, sort_keys=True) + ("\n" if line.endswith("\n") else ""))
            changed = True
        else:
            converted.append(line)
    if changed or recovery.exists():
        if not recovery.exists():
            temporary = recovery.with_suffix(".tmp")
            try:
                with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
                               "w", encoding="utf-8") as output:
                    _ = output.write("".join(converted))
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, recovery)
            finally:
                temporary.unlink(missing_ok=True)
        _ = source.seek(0)
        _ = source.write("".join(converted))
        _ = source.truncate()
        source.flush()
        os.fsync(source.fileno())
        recovery.unlink()
    return converted


def role_history_lines(path: Path) -> list[str]:
    """Read canonical role history while earlier processes may append old names."""
    with path.open("r+", encoding="utf-8") as source:
        with roles_lock():
            fcntl.flock(source.fileno(), fcntl.LOCK_EX)
            return migrate_locked_journal(source, path)


def recover_pending_journal(path: Path, *, role_history: bool = False) -> None:
    """Repair an interrupted conversion before replay readers parse journal bytes."""
    if not path.with_name(f".{path.name}.workers").exists():
        return
    with roles_lock() if role_history else nullcontext():
        with path.open("r+", encoding="utf-8") as source:
            fcntl.flock(source.fileno(), fcntl.LOCK_EX)
            _ = migrate_locked_journal(source, path)

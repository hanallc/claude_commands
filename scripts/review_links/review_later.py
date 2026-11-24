"""Keep one Later time for each Review item across all five stores."""
from __future__ import annotations

import json
import logging
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

from scripts.lib import atomic_file, file_lock
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.user_zone import user_zone
from scripts.production.record_store import state_root
from scripts.review_links import question_path


def path() -> Path:
    return state_root() / 'review-later.json'


def decode_times(content: str) -> dict[str, object]:
    stored = cast(object, json.loads(content))
    if not isinstance(stored, dict):
        raise ValueError('Expected an object of Later times.')
    return cast(dict[str, object], stored)


def stored_times(target: Path) -> dict[str, object]:
    try:
        return decode_times(target.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}


def times(sources: list[Path] | None = None, *, zone: ZoneInfo | None = None,
          skipped: Callable[[Path, str, Exception], None] | None = None) -> dict[str, datetime]:
    target = path()
    if sources is not None:
        sources.append(target)
    valid: dict[str, datetime] = {}
    local_zone = cache(user_zone) if zone is None else lambda: zone
    for key, value in stored_times(target).items():
        try:
            if not isinstance(value, str):
                raise TypeError('A Later time must be text.')
            until = question_path.read_later(value, lambda time: time.replace(tzinfo=local_zone()))
            if isinstance(until, question_path.UntilDelivered):
                raise ValueError('Review Later requires a time.')
            valid[key] = until
        except (TypeError, ValueError) as error:
            if skipped is not None:
                skipped(target, key, error)
            else:
                logging.getLogger(__name__).warning('Review source %s (%s): %s', target, key, error)
    return valid


@dataclass(frozen=True)
class MissingLaterFile:
    """No deadline file existed before the save."""


@contextmanager
def locked() -> Generator[Path]:
    """Hold the deadline lock through a save or a native move."""
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with file_lock.exclusive(target.with_suffix('.lock')):
        yield target


def _write_times(target: Path, stored: dict[str, object]) -> None:
    write_text_atomic(target, json.dumps(stored) + '\n')


@contextmanager
def cleared(key: str) -> Generator[None]:
    """Clear one deadline and restore it if publishing the change fails."""
    if not path().is_file():
        yield
        return
    with locked() as target:
        try:
            previous = target.read_text(encoding='utf-8')
        except FileNotFoundError:
            yield
            return
        stored = decode_times(previous)
        if key not in stored:
            yield
            return
        del stored[key]
        try:
            _write_times(target, stored)
        except atomic_file.AtomicDurabilityUnconfirmed:
            write_text_atomic(target, previous)
            raise
        try:
            yield
        except atomic_file.AtomicDurabilityUnconfirmed:
            raise
        except OSError:
            write_text_atomic(target, previous)
            raise


def move_locked(target: Path, previous: str, current: str) -> None:
    """Move the old deadline under the lock. Keep a newer deadline at the destination."""
    stored = stored_times(target)
    if previous in stored:
        _ = stored.setdefault(current, stored.pop(previous))
        _write_times(target, stored)


def save(key: str, until: datetime, *, record_log: Callable[[Callable[[], None]], None] | None = None) -> None:
    """Let the log writer publish the deadline after its revision check."""
    with locked() as target:
        previous: bytes | MissingLaterFile
        try:
            previous = target.read_bytes()
        except FileNotFoundError:
            previous = MissingLaterFile()
        stored = {} if isinstance(previous, MissingLaterFile) else decode_times(previous.decode('utf-8'))
        stored[key] = question_path.write_later(until)
        published = False
        confirmed = False

        def publish() -> None:
            nonlocal published, confirmed
            try:
                _write_times(target, stored)
            except atomic_file.AtomicDurabilityUnconfirmed:
                published = True
                raise
            published = True
            confirmed = True

        try:
            try:
                if record_log is None:
                    publish()
                else:
                    record_log(publish)
            except atomic_file.AtomicDurabilityUnconfirmed as error:
                if not confirmed:
                    raise
                # The log writer only reports this error if its replacement stays visible.
                logging.getLogger(__name__).warning('Review Later log durability %s: %s', key, error)
        except BaseException:
            if published:
                if isinstance(previous, MissingLaterFile):
                    target.unlink()
                    atomic_file.sync_folder(target.parent)
                else:
                    atomic_file.write_bytes_atomic(target, previous)
            raise


def deadline() -> str:
    zone = user_zone()
    now = datetime.now(zone)
    future = [value for value in times(zone=zone).values() if value > now]
    return str(int(min(future).timestamp())) if future else ''

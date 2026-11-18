"""Keep one Later time for each Review item across all five stores."""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

from scripts.lib import atomic_file, file_lock
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.user_zone import user_zone
from scripts.production.record_store import state_root


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


def times(sources: list[Path] | None = None, *,
          skipped: Callable[[Path, str, Exception], None] | None = None) -> dict[str, datetime]:
    target = path()
    if sources is not None:
        sources.append(target)
    valid: dict[str, datetime] = {}
    zone = user_zone()
    for key, value in stored_times(target).items():
        try:
            if not isinstance(value, str):
                raise TypeError('A Later time must be text.')
            until = datetime.fromisoformat(value)
            valid[key] = until if until.tzinfo is not None else until.replace(tzinfo=zone)
        except (TypeError, ValueError) as error:
            if skipped is not None:
                skipped(target, key, error)
            else:
                logging.getLogger(__name__).warning('Review source %s (%s): %s', target, key, error)
    return valid


@dataclass(frozen=True)
class MissingLaterFile:
    """No deadline file existed before the save."""


def save(key: str, until: datetime, *, record_log: Callable[[Callable[[], None]], None] | None = None) -> None:
    """Let the log writer publish the deadline after its revision check."""
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with file_lock.exclusive(target.with_suffix('.lock')):
        previous: bytes | MissingLaterFile
        try:
            previous = target.read_bytes()
        except FileNotFoundError:
            previous = MissingLaterFile()
        stored = {} if isinstance(previous, MissingLaterFile) else decode_times(previous.decode('utf-8'))
        stored[key] = until.isoformat()
        published = False
        confirmed = False

        def publish() -> None:
            nonlocal published, confirmed
            try:
                write_text_atomic(target, json.dumps(stored)+'\n')
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
    now = datetime.now().astimezone(user_zone())
    future = [value for value in times().values() if value > now]
    return str(int(min(future).timestamp())) if future else ''

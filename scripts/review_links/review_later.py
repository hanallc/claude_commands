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


def times(sources: list[Path] | None = None) -> dict[str, datetime]:
    target = path()
    if sources is not None:
        sources.append(target)
    try:
        stored = cast(dict[str, str], json.loads(target.read_text()))
    except FileNotFoundError:
        return {}
    return {key: datetime.fromisoformat(value) for key, value in stored.items()}


@dataclass(frozen=True)
class MissingLaterFile:
    """No deadline file existed before the save."""


def save(key: str, until: datetime, *, record_log: Callable[[], object] = lambda: None) -> None:
    """Save the deadline before the log. Restore it if the log fails to publish."""
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with file_lock.exclusive(target.with_suffix('.lock')):
        previous: bytes | MissingLaterFile
        try:
            previous = target.read_bytes()
        except FileNotFoundError:
            previous = MissingLaterFile()
        stored = {} if isinstance(previous, MissingLaterFile) else cast(dict[str, str], json.loads(previous))
        stored[key] = until.isoformat()
        published = False
        try:
            try:
                write_text_atomic(target, json.dumps(stored)+'\n')
            except atomic_file.AtomicDurabilityUnconfirmed:
                published = True
                raise
            published = True
            try:
                _ = record_log()
            except atomic_file.AtomicDurabilityUnconfirmed as error:
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

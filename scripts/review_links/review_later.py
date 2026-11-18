"""Keep one Later time for each Review item across all five stores."""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import cast

from scripts.lib import file_lock
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.user_zone import user_zone
from scripts.production.record_store import state_root


def path() -> Path:
    return state_root() / 'review-later.json'


def stored_times(target: Path) -> dict[str, object]:
    try:
        stored = cast(object, json.loads(target.read_text(encoding='utf-8')))
    except FileNotFoundError:
        return {}
    if not isinstance(stored, dict):
        raise ValueError('Expected an object of Later times.')
    return cast(dict[str, object], stored)


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


def save(key: str, until: datetime, *, check: Callable[[], object] | None = None) -> None:
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with file_lock.exclusive(target.with_suffix('.lock')):
        stored = stored_times(target)
        if check is not None:
            _ = check()
        stored[key] = until.isoformat()
        write_text_atomic(target, json.dumps(stored)+'\n')


def deadline() -> str:
    now = datetime.now().astimezone(user_zone())
    future = [value for value in times().values() if value > now]
    return str(int(min(future).timestamp())) if future else ''

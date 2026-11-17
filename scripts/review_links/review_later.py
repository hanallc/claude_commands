"""Keep one Later time for each Review item across all five stores."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import cast

from scripts.lib import file_lock
from scripts.lib.atomic_file import write_text_atomic
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


def save(key: str, until: datetime) -> None:
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with file_lock.exclusive(target.with_suffix('.lock')):
        stored = times()
        stored[key] = until
        write_text_atomic(target, json.dumps({key:value.isoformat() for key,value in stored.items()})+'\n')


def deadline() -> str:
    now = datetime.now().astimezone()
    future = [value for value in times().values() if value > now]
    return str(int(min(future).timestamp())) if future else ''

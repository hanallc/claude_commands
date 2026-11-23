"""Stop the AWS calls of the controller after AWS refuses its key.

A refused key refuses every later call too, so each tick would only repeat the refusal.
The record holds the calls until a tick sees every AWS machine off and a later tick sees one on.
The hourly job and the cloud gate route read the record too, so they also make no call while it exists.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import cast

from scripts.aws_target import ledger
from scripts.lib.atomic_file import write_text_atomic

RECORD_NAME = 'key_refused.json'
UNREADABLE_CODE = 'unreadable refusal record'
REFUSED_REASON = 'AWS refused the key; no AWS call until a person turns every AWS machine off and then one on'


@dataclass(frozen=True)
class KeyRefused:
    """AWS refused the key; `off_seen` records that a tick saw every AWS machine off after the refusal."""

    code: str
    at: int
    off_seen: bool


@dataclass(frozen=True)
class NoKeyRefusal:
    """No refusal is recorded, so the controller may call AWS."""


def read(directory: Path) -> KeyRefused | NoKeyRefusal:
    """Read the refusal; an unreadable record holds the calls, as a refusal does."""
    try:
        row = ledger.mapping(cast(object, json.loads((directory / RECORD_NAME).read_text())))
        off_seen = row.get('off_seen')
        if not isinstance(off_seen, bool):
            raise ValueError('off_seen is not a boolean')
        return KeyRefused(ledger.text(row, 'code'), ledger.instant(row, 'at'), off_seen)
    except FileNotFoundError:
        return NoKeyRefusal()
    except (OSError, ValueError):
        return KeyRefused(UNREADABLE_CODE, 0, False)


def write(directory: Path, refusal: KeyRefused) -> None:
    """Replace the record with this refusal."""
    write_text_atomic(directory / RECORD_NAME, json.dumps(
        {'code': refusal.code, 'at': refusal.at, 'off_seen': refusal.off_seen}, sort_keys=True) + '\n')


def clear(directory: Path) -> None:
    (directory / RECORD_NAME).unlink(missing_ok=True)

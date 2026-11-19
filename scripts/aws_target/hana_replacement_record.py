"""Store the Hana root replacement hold."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from scripts.aws_target import ledger, remote
from scripts.lib.atomic_file import write_text_atomic


RESUME = 'Run deploy.py hana-setup --approve-resources again.'
HELD = 'Hana root replacement holds the box. ' + RESUME


class ReplacementRefused(ValueError):
    """Hana root replacement requires more evidence or another approved run."""


@dataclass(frozen=True)
class NoReplacement:
    """No replacement holds the box."""


@dataclass(frozen=True)
class ReplacementStopDuty:
    """Identify the held reservation that the controller must stop."""
    generation: int
    token: str


@dataclass(frozen=True)
class ReplacementAttempt:
    """Keep the registration, attachment, token and reservation for one replacement."""
    image: str
    attached_at: str
    token: str
    generation: int
    guest_digest: str


def record_path(directory: Path) -> Path:
    return directory / 'hana' / 'replacement.json'


def held(directory: Path) -> bool:
    try:
        _ = record_path(directory).lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _read_row(directory: Path) -> NoReplacement | dict[str, object]:
    try:
        row = remote.read_record(record_path(directory))
    except FileNotFoundError:
        return NoReplacement()
    if type(row.get('version')) is not int or row['version'] != 2:
        raise ReplacementRefused('The Hana replacement record has an unknown version. Review the saved deployment.')
    return row


def read_stop_duty(directory: Path) -> NoReplacement | ReplacementStopDuty:
    row = _read_row(directory)
    if isinstance(row, NoReplacement):
        return row
    return ReplacementStopDuty(ledger.instant(row, 'generation'), ledger.text(row, 'token'))


def read(directory: Path) -> NoReplacement | ReplacementAttempt:
    row = _read_row(directory)
    if isinstance(row, NoReplacement):
        return row
    return ReplacementAttempt(ledger.text(row, 'image'), ledger.text(row, 'attached_at'), ledger.text(row, 'token'),
                              ledger.instant(row, 'generation'), ledger.text(row, 'guest_digest'))


def save(directory: Path, attempt: ReplacementAttempt) -> None:
    row: dict[str, object] = dict(version=2, image=attempt.image, attached_at=attempt.attached_at,
                                  token=attempt.token, generation=attempt.generation, guest_digest=attempt.guest_digest)
    write_text_atomic(record_path(directory), json.dumps(row, sort_keys=True) + '\n', new_file_mode=0o600)

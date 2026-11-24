"""Keep one main goal and its ready card in each producer's records."""

from __future__ import annotations

try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import json
import os
import secrets
import sys
from collections.abc import Callable, Generator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from enum import StrEnum
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

from scripts.lib.atomic_file import AtomicDurabilityUnconfirmed, sync_folder, write_text_atomic
from scripts.lib.file_lock import exclusive
from scripts.lib.showrunner_state import state_root

if TYPE_CHECKING:
    from scripts.production import staff


STALE_CARD = "Card is stale: run goal.py card."


class NoGoal(StrEnum):
    UNSET = "No main goal set."


class ItemAvailability(StrEnum):
    UNAVAILABLE = "List item unavailable"


class GoalRecord(TypedDict):
    keeper: staff.StaffId
    item: str
    blocked: str
    faster: str
    noted_at: str


class InvalidGoalRecord(ValueError):
    """The saved goal does not contain valid goal data."""


def goal_path(producer: staff.StaffId) -> Path:
    """Find the saved goal for this producer."""
    if not producer or Path(producer).name != producer or producer in (".", ".."):
        raise ValueError("Invalid producer staff ID.")
    return state_root() / producer / "main-goal.json"


def card_path(producer: staff.StaffId) -> Path:
    """Find the ready card for this producer."""
    return goal_path(producer).with_name("main-goal-card.json")


def _lock_path(producer: staff.StaffId) -> Path:
    return goal_path(producer).with_name(".main-goal.lock")


@contextmanager
def locked(producer: staff.StaffId) -> Generator[None]:
    """Lock the producer's goal across all List stores."""
    path = _lock_path(producer)
    path.parent.mkdir(parents=True, exist_ok=True)
    with exclusive(path):
        yield


def _read_json(path: Path) -> object:
    return cast(object, json.loads(path.read_text(encoding="utf-8")))


def load(producer: staff.StaffId) -> GoalRecord | NoGoal:
    """Read the producer's main goal record."""
    try:
        value = _read_json(goal_path(producer))
    except FileNotFoundError:
        return NoGoal.UNSET
    except (ValueError, RecursionError) as error:
        raise InvalidGoalRecord("Invalid main goal record.") from error
    if isinstance(value, dict):
        fields = cast(dict[str, object], value)
        record = {key: fields.get(key) for key in ("keeper", "item", "blocked", "faster", "noted_at")}
        if all(isinstance(field, str) for field in record.values()):
            return cast(GoalRecord, cast(object, record))
    raise InvalidGoalRecord("Invalid main goal record.")


def read_card(producer: staff.StaffId) -> str:
    """Read the ready card without reading The List."""
    try:
        value = _read_json(card_path(producer))
    except FileNotFoundError:
        return NoGoal.UNSET.value
    if isinstance(value, dict):
        card = cast(dict[str, object], value).get("card")
        if isinstance(card, str) and card:
            return card
    raise ValueError("Invalid main goal card.")


@dataclass(frozen=True)
class ItemStatus:
    title: str
    state: str
    eta: str


def _write_card(producer: staff.StaffId, card: str) -> None:
    write_text_atomic(card_path(producer), json.dumps({"card": card}, ensure_ascii=False) + "\n", new_file_mode=0o600)


def write_goal(producer: staff.StaffId, record: GoalRecord) -> None:
    """Mark the card stale before saving the goal."""
    _write_card(producer, STALE_CARD)
    write_text_atomic(goal_path(producer), json.dumps(record, ensure_ascii=False) + "\n", new_file_mode=0o600)


def clear_card(producer: staff.StaffId) -> None:
    """Remove a card when the producer has no saved goal."""
    path = card_path(producer)
    try:
        path.unlink()
    except FileNotFoundError:
        return
    sync_folder(path.parent)


def save(producer: staff.StaffId, saved: GoalRecord, status: ItemStatus | ItemAvailability) -> str:
    """Publish the ready card from the saved goal and List status."""
    label, state, eta = saved["item"], ItemAvailability.UNAVAILABLE.value, "none recorded"
    if isinstance(status, ItemStatus):
        label += f" {status.title}"
        state, eta = status.state, status.eta
    card = "\n".join((
        f"Main goal: {label}",
        f"State: {state}",
        f"ETA: {eta}",
        f"Blocked by: {saved['blocked'] or 'none recorded'}",
        f"Faster if: {saved['faster'] or 'none recorded'}",
        f"Last note: {saved['noted_at'] or 'none recorded'}",
    ))
    _write_card(producer, card)
    return card


def _report_error(owner: staff.StaffId, error: Exception) -> None:
    detail = " ".join(str(error).splitlines())
    action = "" if isinstance(error, InvalidGoalRecord) else " Run goal.py card to refresh it."
    print(f"Main goal for {owner}: {detail}{action}", file=sys.stderr)


@contextmanager
def _mark_stale(producer: staff.StaffId) -> Generator[Path | None]:
    """Preserve the old card without reading it and publish the stale line."""
    path = card_path(producer)
    backup = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        backup.hardlink_to(path)
    except FileNotFoundError:
        try:
            _ = goal_path(producer).stat()
        except FileNotFoundError:
            yield None
            return
    try:
        _write_card(producer, STALE_CARD)
        yield backup
    finally:
        try:
            backup.unlink(missing_ok=True)
        except OSError as error:
            print(f"Main goal for {producer}: Cannot remove the card backup. {error}", file=sys.stderr)


def _restore_card(producer: staff.StaffId, backup: Path) -> None:
    path = card_path(producer)
    try:
        _ = backup.replace(path)
    except FileNotFoundError:
        clear_card(producer)
    else:
        sync_folder(path.parent)


@contextmanager
def list_item_change(keeper: staff.StaffId, identity: str, producer: staff.StaffId,
                     status: ItemStatus, previous_producer: staff.StaffId | None) -> Generator[None]:
    """Mark affected cards stale before the List write, then refresh the matching card."""
    producers = (producer,) if previous_producer in (None, producer) else (producer, previous_producer)
    with ExitStack() as preserved:
        backups: dict[staff.StaffId, Path] = {}

        def finish(*, published: bool) -> None:
            for owner, backup in backups.items():
                try:
                    if not published:
                        _restore_card(owner, backup)
                        continue
                    saved = load(owner)
                    if saved is NoGoal.UNSET:
                        clear_card(owner)
                    elif (saved["keeper"], saved["item"]) == (keeper, identity):
                        _ = save(owner, saved, status if owner == producer else ItemAvailability.UNAVAILABLE)
                    else:
                        _restore_card(owner, backup)
                except (OSError, ValueError, RecursionError) as error:
                    _report_error(owner, error)

        published = False
        try:
            for owner in sorted(producers):
                try:
                    _ = preserved.enter_context(exclusive(_lock_path(owner)))
                except FileNotFoundError:
                    # A first mark for this item must acquire the List lock.
                    continue
                backup = preserved.enter_context(_mark_stale(owner))
                if backup is not None:
                    backups[owner] = backup
            try:
                yield
            except AtomicDurabilityUnconfirmed:
                published = True
                raise
        except (OSError, ValueError):
            finish(published=published)
            raise
        else:
            finish(published=True)


if __name__ == "__main__":
    main = cast(Callable[[], int], import_module("scripts.production.the_list").goal_main)
    raise SystemExit(main())

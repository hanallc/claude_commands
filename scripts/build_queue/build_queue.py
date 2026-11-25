#!/usr/bin/env python3
"""Change a waiting unit's build priority."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from dataclasses import dataclass
import os
from pathlib import Path
import sys
import time
import uuid

from scripts.build_hold.override_guard import SharedStateWriteRefused
from scripts.build_queue import admission_protocol, admission_records, person_actions, queue_snapshot
from scripts.buildlog import store
from scripts.lint import memory_admit
from scripts.message import unit_labels

USAGE = "Usage: /build:bump <position|session|folder> | clear [unit]"


def record(directory: Path, action: person_actions.SetBump | person_actions.ClearBump,
           unit: str, actor: str, reason: str) -> int:
    bump = person_actions.Bump(uuid.uuid4().hex, action, unit, actor, time.time(), reason)
    person_actions.save_bump(directory, bump)
    print(f"build queue: recorded {bump.id}")
    return 0


@dataclass(frozen=True)
class SelectedStep:
    row: admission_records.Waiter
    place: int


def waiting_positions(reading: admission_records.QueueRead) -> dict[int, admission_records.WaitingStep]:
    """Use the same place numbers as the Builds page."""
    kept = {place.position for place in reading.numbered_held_places(memory_admit.waiter_order)}
    waiting = iter(reading.view.waiting)
    return {position: next(waiting) for position in range(1, len(reading.view.waiting) + len(kept) + 1)
            if position not in kept}


def select_step(directory: Path, selector: str) -> SelectedStep | admission_records.HeldPlace | admission_records.NoWaiter:
    numeric = selector.isdecimal()
    try:
        reading = queue_snapshot.read_view_snapshot(directory).reading
    except admission_protocol.AdmissionUnavailable:
        if numeric:
            raise admission_protocol.AdmissionUnavailable("a numeric selector requires the published queue view") from None
        snapshot = queue_snapshot.read_queue_snapshot(directory)
        if isinstance(snapshot, queue_snapshot.NoQueueSnapshot):
            raise admission_protocol.AdmissionUnavailable("no published queue order") from None
        rows = memory_admit.ranked_queue_waiters(snapshot.inputs)
        kept = {}
        positions = {place: SelectedStep(row, place) for place, (_, row) in enumerate(rows, 1)}
    else:
        rows = reading.ordered
        kept = {place.position: place for place in reading.numbered_held_places(memory_admit.waiter_order)}
        ranked = {(row["pid"], row["since"]): row for _, row in rows}
        positions = {place: SelectedStep(row, place) for place, step in waiting_positions(reading).items()
                     if (row := ranked.get((step.pid, step.since))) is not None}
    if numeric:
        position = int(selector)
        if position in kept:
            return kept[position]
        return positions.get(position, admission_records.NoWaiter())
    labels = unit_labels.unit_labels_for([row.get("session_id", "") for _, row in rows], os.environ)
    for selected in positions.values():
        session = selected.row.get("session_id", "")
        label = labels.get(session)
        if selector == session or (label is not None and selector == label.session):
            return selected
    for selected in positions.values():
        folder = selected.row.get("folder", "")
        if selector in (folder, Path(folder).name):
            return selected
        if selector == selected.row.get("session_name"):
            return selected
    return admission_records.NoWaiter()


@dataclass(frozen=True)
class BumpCommand:
    words: list[str]
    why: str | admission_protocol.WordsNotGiven


@dataclass(frozen=True)
class UsageRefused:
    text: str


def parse_command(arguments: list[str]) -> BumpCommand | UsageRefused:
    words: list[str] = []
    why: str | admission_protocol.WordsNotGiven = admission_protocol.WORDS_NOT_GIVEN
    remaining = iter(arguments)
    for argument in remaining:
        if argument == "--words":
            given = next(remaining, None)
            if given is None:
                return UsageRefused("build bump: --words needs the user's words")
            why = given
        elif argument.startswith("--words="):
            why = argument.removeprefix("--words=")
        elif argument == "--run":
            return UsageRefused("build bump: use /build:bump <position|session|folder>")
        else:
            words.append(argument)
    return BumpCommand(words, why)


def bump(selector: str, actor: str, why: str) -> int:
    directory = store.root() / "admission"
    selected = select_step(directory, selector)
    if isinstance(selected, admission_records.HeldPlace):
        print(f"build queue: place #{selected.position} is kept for {selected.unit}")
        return 3
    if isinstance(selected, admission_records.NoWaiter):
        print(f"build queue: no waiting step matches {selector}")
        return 3
    row = selected.row
    session = row.get("session_id", "")
    unit = row.get("unit", "")
    if not unit and session:
        unit = memory_admit.queue_unit(unit, session, memory_admit.priority_labels([session]))
    if not unit or unit in (unit_labels.OWNER_NOT_RECORDED, "—"):
        print(f"build queue: waiting step {selector} has no unit")
        return 3
    step_key = row.get("move_id", "")
    if not step_key:
        print(f"build queue: waiting step {selector} has no queue key")
        return 3
    return record(directory, person_actions.SetBump(step_key, selected.place), unit, actor, why)


def main() -> int:
    command = parse_command(sys.argv[1:])
    if isinstance(command, UsageRefused):
        print(command.text)
        return 3
    words = command.words
    if not words or words == ["--help"]:
        from scripts.build_queue.memory_feed import page_link

        print(USAGE)
        return page_link()
    if words[0] == "clear":
        if len(words) > 2:
            print("build bump: use /build:bump clear [unit]")
            return 3
    elif len(words) != 1 or len(words[0].split()) != 1:
        print("build bump: use /build:bump <position|session|folder>")
        return 3
    try:
        actor = admission_records.queue_command_actor()
        why = person_actions.asked_words(command.why, actor)
        if isinstance(why, admission_protocol.Refused):
            print(f"build queue: {why.reason}")
            return 3
        if words[0] != "clear":
            return bump(words[0], actor, why)
        unit = words[1] if len(words) == 2 else ""
        return record(store.root() / "admission", person_actions.ClearBump(), unit, actor, why)
    except person_actions.UnknownBumpUnit as error:
        print(f"build queue: {error}")
        return 3
    except admission_protocol.AdmissionUnavailable as error:
        print(f"build queue: {error.reason}", file=sys.stderr)
        return 2
    except (OSError, ValueError, SharedStateWriteRefused) as error:
        print(f"build queue: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

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


def select_step(directory: Path, selector: str) -> SelectedStep | admission_records.NoWaiter:
    if not selector.isdecimal():
        try:
            published = queue_snapshot.read_view_snapshot(directory)
        except admission_protocol.AdmissionUnavailable:
            pass
        else:
            rows = published.reading.ordered
            labels = unit_labels.unit_labels_for([row.get("session_id", "") for _, row in rows], os.environ)
            for place, (_, row) in enumerate(rows, 1):
                session = row.get("session_id", "")
                label = labels.get(session)
                if selector == session or (label is not None and selector == label.session):
                    return SelectedStep(row, place)
    snapshot = queue_snapshot.read_queue_snapshot(directory)
    if isinstance(snapshot, queue_snapshot.NoQueueSnapshot):
        raise admission_protocol.AdmissionUnavailable("no published queue order")
    rows = memory_admit.ranked_queue_waiters(snapshot.inputs)
    if selector.isdecimal():
        position = int(selector)
        return SelectedStep(rows[position - 1][1], position) if 1 <= position <= len(rows) else admission_records.NoWaiter()
    for place, (_, row) in enumerate(rows, 1):
        folder = row.get("folder", "")
        if selector in (folder, Path(folder).name):
            return SelectedStep(row, place)
    return admission_records.NoWaiter()


def bump(selector: str, actor: str) -> int:
    directory = store.root() / "admission"
    selected = select_step(directory, selector)
    if isinstance(selected, admission_records.NoWaiter):
        print(f"build queue: no waiting step matches {selector}")
        return 3
    row = selected.row
    session = row.get("session_id", "")
    unit = row.get("unit", "")
    if not unit and session:
        unit = memory_admit.queue_unit(unit, session, memory_admit.priority_labels([session]))
    if not unit or unit == unit_labels.OWNER_NOT_RECORDED:
        print(f"build queue: waiting step {selector} has no unit")
        return 3
    step_key = row.get("move_id", "")
    if not step_key:
        print(f"build queue: waiting step {selector} has no queue key")
        return 3
    return record(directory, person_actions.SetBump(step_key, selected.place), unit, actor, "the user's bump")


def main() -> int:
    words = sys.argv[1:]
    if not words or words == ["--help"]:
        from scripts.build_queue.memory_feed import page_link

        print("Usage: /build:bump <position|session|folder> | clear [unit]")
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
        if words[0] != "clear":
            return bump(words[0], actor)
        unit = words[1] if len(words) == 2 else ""
        return record(store.root() / "admission", person_actions.ClearBump(), unit, actor, "the user's clear")
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

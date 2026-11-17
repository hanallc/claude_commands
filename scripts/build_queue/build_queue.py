#!/usr/bin/env python3
"""Change a waiting unit's build priority."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from dataclasses import dataclass

import os
import sys

from scripts.build_queue import admission_client, admission_protocol, admission_records, queue_evidence
from scripts.lint import memory_admit
from scripts.message import unit_labels


@dataclass(frozen=True)
class WaitingPosition:
    place: int


@dataclass(frozen=True)
class NoWaitingStep:
    pass


def bump(selector: str, actor: str) -> int:
    reading = memory_admit.queue_read()
    labels = unit_labels.unit_labels_for([step.session_id for step in reading.view.waiting], os.environ)
    matches = [position for position, step in enumerate(reading.view.waiting, 1)
               if queue_evidence.step_labels(step, labels).session == selector]
    if not matches:
        matches = memory_admit.match_waiters(selector, reading.view)
    if not matches:
        print(f"build queue: no waiting step matches {selector}")
        return 3
    selected_place = matches[0]
    step = reading.view.waiting[selected_place - 1]
    unit = memory_admit.queue_unit(step.unit, step.session_id, labels)
    if unit == unit_labels.UnitLabels("").unit or not unit:
        print(f"build queue: waiting step #{selected_place} has no unit")
        return 3
    result = admission_client.priority_set(unit, "the user's bump", actor)
    if isinstance(result, admission_protocol.Refused):
        print(f"build queue: {result.reason}")
        return 3
    reading = memory_admit.queue_read()
    first = next((WaitingPosition(place) for place, step in enumerate(reading.view.waiting, 1)
                  if memory_admit.queue_unit(step.unit, step.session_id, labels) == unit), NoWaitingStep())
    detail = f"first step now #{first.place}" if isinstance(first, WaitingPosition) else "no waiting step now"
    priority = reading.explanation.priority
    units = ([entry["unit"] for entry in priority.entries]
             if isinstance(priority, admission_records.QueuePriority) else [])
    place = f"; priority #{units.index(unit) + 1}" if unit in units else ""
    print(f"queue priority set for {unit} (step #{selected_place}): {detail}{place}")
    return 0


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
        result = (admission_client.priority_clear(actor, words[1]) if len(words) == 2
                  else admission_client.priority_clear(actor))
        if isinstance(result, admission_protocol.Refused):
            print(f"build queue: {result.reason}")
            return 3
        print("queue priority cleared")
        return 0
    except admission_protocol.AdmissionUnavailable as error:
        print(f"build queue: admission service unavailable: {error.reason}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as error:
        print(f"build queue: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

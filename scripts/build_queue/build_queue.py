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

from scripts.build_queue import admission_client, admission_protocol, admission_records, head_facts, queue_evidence
from scripts.build_queue.scope_activity import LiveBuildRun, NoLiveBuildRun
from scripts.lint import memory_admit
from scripts.message import unit_labels

USAGE = "Usage: /build:bump [--run] <position|session|folder> | clear [unit]"


@dataclass(frozen=True)
class WaitingPosition:
    place: int


@dataclass(frozen=True)
class NoWaitingStep:
    pass


@dataclass(frozen=True)
class WholeUnitBump:
    """The bump raises every step of the selected step's unit."""


@dataclass(frozen=True)
class OneRunBump:
    """The bump raises only the run of the selected step."""


@dataclass(frozen=True)
class BumpCommand:
    """The words of the command line: the selector or clear words, the bump scope, and the user's words."""
    words: list[str]
    scope: WholeUnitBump | OneRunBump
    why: str | admission_protocol.WordsNotGiven


@dataclass(frozen=True)
class UsageRefused:
    text: str


def parse_command(arguments: list[str]) -> BumpCommand | UsageRefused:
    """Take --words and --run out of the arguments; keep each other word, a dash word too."""
    words: list[str] = []
    scope: WholeUnitBump | OneRunBump = WholeUnitBump()
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
            scope = OneRunBump()
        else:
            words.append(argument)
    return BumpCommand(words, scope, why)


def bump(selector: str, actor: str, why: str | admission_protocol.WordsNotGiven,
         scope: WholeUnitBump | OneRunBump) -> int:
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
    if isinstance(scope, OneRunBump):
        if not isinstance(step.run, LiveBuildRun):
            print(f"build queue: waiting step #{selected_place} has no run; bump its unit without --run")
            return 3
        run = admission_protocol.RunIdentity(step.run.pid, step.run.start_time)
        target: admission_protocol.WholeUnit | admission_protocol.OneRun = admission_protocol.OneRun(run, step.step)
        named = f"{unit} · {step.step} (one run)"
    else:
        target, named = admission_protocol.WHOLE_UNIT, unit
    result = admission_client.priority_set(unit, why, actor, target)
    if isinstance(result, admission_protocol.Refused):
        print(f"build queue: {result.reason}")
        return 3
    reading = memory_admit.queue_read()
    first = next((WaitingPosition(place) for place, step in enumerate(reading.view.waiting, 1)
                  if bump_covers(target, unit, memory_admit.queue_unit(step.unit, step.session_id, labels), step.run)),
                 NoWaitingStep())
    detail = f"first step now #{first.place}" if isinstance(first, WaitingPosition) else "no waiting step now"
    priority = reading.explanation.priority
    entries = priority.entries if isinstance(priority, admission_records.QueuePriority) else ()
    place = next((f"; priority #{index}" for index, entry in enumerate(entries, 1)
                  if entry_is_target(entry, target, unit)), "")
    print(f"queue priority set for {named} (step #{selected_place}): {detail}{place}")
    if text := head_facts.priority_text(priority):
        print(text)
    return 0


def bump_covers(target: admission_protocol.WholeUnit | admission_protocol.OneRun, unit: str, step_unit: str,
                run: LiveBuildRun | NoLiveBuildRun) -> bool:
    if isinstance(target, admission_protocol.OneRun):
        return isinstance(run, LiveBuildRun) and admission_protocol.RunIdentity(run.pid, run.start_time) == target.run
    return step_unit == unit


def entry_is_target(entry: admission_records.PriorityEntry,
                    target: admission_protocol.WholeUnit | admission_protocol.OneRun, unit: str) -> bool:
    if admission_records.is_run_entry(entry):
        return isinstance(target, admission_protocol.OneRun) and entry["run"] == target.run
    return isinstance(target, admission_protocol.WholeUnit) and entry["unit"] == unit


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
        if len(words) > 2 or isinstance(command.scope, OneRunBump):
            print("build bump: use /build:bump clear [unit]")
            return 3
    elif len(words) != 1 or len(words[0].split()) != 1:
        print("build bump: use /build:bump <position|session|folder>")
        return 3
    try:
        actor = admission_records.queue_command_actor()
        if words[0] != "clear":
            return bump(words[0], actor, command.why, command.scope)
        result = admission_client.priority_clear(actor, words[1] if len(words) == 2 else "", command.why)
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

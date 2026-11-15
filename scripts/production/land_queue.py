"""The line of checkpoint lands that wait for the merge gate.

The land runner takes each item with `next`, in order. Each item has a label, which is the runner's own
line, and the commits it lands: the third field of each line of its batch file, and each `--commit`.
The units board reads the line to show the place of a checkpoint that waits to land.

Usage:
  land_queue.py add LABEL [--batch PATH] [--commit HASH ...] [--first | --before LABEL]
  land_queue.py move LABEL (--first | --before LABEL)
  land_queue.py remove LABEL
  land_queue.py next
  land_queue.py list
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple, TypedDict, cast

from scripts.lib import file_lock
from scripts.lib.atomic_file import write_text_atomic
from scripts.production import land_runs, waits_on

HASH = re.compile(r"[0-9a-f]{7,40}")


class LineItem(TypedDict):
    label: str
    batch: str
    commits: list[str]
    added: str


class BatchCommit(NamedTuple):
    unit: str
    phase: str
    commit: str


class NoLine(NamedTuple):
    """No land runner has used the line yet."""


class Place(NamedTuple):
    """A commit's place in the line; 1 lands next. The owner is the batch line's unit and phase, else the label."""

    number: int
    label: str
    owner: str


class LandView(NamedTuple):
    """The lands that run now, the land line, and the newest result of each land.

    A land also takes each commit its listed commit holds that is not on main: carried_running and carried_line.
    """

    running: frozenset[str]
    line: dict[str, Place] | NoLine
    results: dict[str, land_runs.LandResult]
    carried_running: frozenset[str]
    carried_line: dict[str, Place]


class LandRunning(NamedTuple):
    pass


class InLine(NamedTuple):
    """The commit waits at a place; failed is the newest result when that try did not land."""

    place: Place
    failed: land_runs.LandResult | None


class LandEnded(NamedTuple):
    result: land_runs.LandResult


class NotInLine(NamedTuple):
    pass


class LineUnused(NamedTuple):
    """No runner uses the line, and the commit has no land result."""


LandState = LandRunning | InLine | LandEnded | NotInLine | LineUnused


class Refusal(Exception):
    pass


def line_path() -> Path:
    return land_runs.state_dir() / "land_queue.json"


def read_items() -> list[LineItem] | NoLine:
    try:
        text = line_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return NoLine()
    return cast(list[LineItem], json.loads(text))


@contextmanager
def edited() -> Generator[list[LineItem]]:
    """Hold the line lock, yield the items to change, and write them when the block ends."""
    path = line_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock.exclusive(path.with_name(path.name + ".lock")):
        read = read_items()
        items = [] if isinstance(read, NoLine) else read
        yield items
        write_text_atomic(path, json.dumps(items, indent=1) + "\n")


def batch_commits(batch: str) -> tuple[BatchCommit, ...]:
    """The unit, phase and commit of each batch line; a line whose third field is not a hash is not a commit."""
    if not batch:
        return ()
    try:
        lines = Path(batch).read_text(encoding="utf-8").splitlines()
    except OSError:
        return ()
    found: list[BatchCommit] = []
    for line in lines:
        fields = line.split()
        if len(fields) >= 3 and HASH.fullmatch(fields[2]):
            found.append(BatchCommit(fields[0], fields[1], fields[2]))
    return tuple(found)


def places() -> dict[str, Place] | NoLine:
    """The place of each commit in the line; an unreadable line is an empty line."""
    found: dict[str, Place] = {}
    try:
        items = read_items()
        if isinstance(items, NoLine):
            return items
        for number, item in enumerate(items, 1):
            for commit in item["commits"]:
                _ = found.setdefault(commit, Place(number, item["label"], item["label"]))
            for entry in batch_commits(item["batch"]):
                _ = found.setdefault(entry.commit, Place(number, item["label"], f"{entry.unit} {entry.phase}"))
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    return found


def place_of(line: dict[str, Place], commit: str) -> Place | None:
    """The first place whose commit names this commit; an item can name a short hash."""
    found = [place for listed, place in line.items() if land_runs.holds(frozenset({listed}), commit)]
    return min(found, key=lambda place: place.number) if found else None


def land_view(repo: Path) -> LandView:
    """Read the lands; one git call for each listed commit finds the commits it carries."""
    running, line = land_runs.running_commits(), places()
    carried_running = frozenset(commit for listed in running for commit in waits_on.carried(repo, listed))
    carried_line: dict[str, Place] = {}
    if not isinstance(line, NoLine):
        for listed, place in sorted(line.items(), key=lambda entry: entry[1].number):
            for commit in waits_on.carried(repo, listed):
                _ = carried_line.setdefault(commit, place)
    return LandView(running, line, land_runs.last_results(), carried_running, carried_line)


def land_state(view: LandView, commit: str) -> LandState:
    """Where a commit is on its way to land: running, at a place in the line, or ended with its newest result."""
    if land_runs.holds(view.running, commit):
        return LandRunning()
    result = land_runs.result_for(view.results, commit)
    failed = result if result is not None and not result["landed"] else None
    place = None if isinstance(view.line, NoLine) else place_of(view.line, commit)
    if place is not None:
        return InLine(place, failed)
    if result is not None and result["landed"]:
        return LandEnded(result)
    if land_runs.holds(view.carried_running, commit):
        return LandRunning()
    if (carrier := place_of(view.carried_line, commit)) is not None:
        return InLine(carrier, failed)
    if result is not None:
        return LandEnded(result)
    return LineUnused() if isinstance(view.line, NoLine) else NotInLine()


def position(items: list[LineItem], label: str) -> int:
    for index, item in enumerate(items):
        if item["label"] == label:
            return index
    raise Refusal(f"no item {label!r} in the land line")


def insert(items: list[LineItem], item: LineItem, *, first: bool, before: str | None) -> None:
    index = 0 if first else position(items, before) if before is not None else len(items)
    items.insert(index, item)


def add(label: str, *, batch: str, commits: list[str], first: bool, before: str | None) -> None:
    if batch and not batch_commits(batch):
        raise Refusal(f"batch file {batch} names no commit")
    if not batch and not commits:
        raise Refusal("give --batch or --commit")
    bad = [commit for commit in commits if not HASH.fullmatch(commit)]
    if bad:
        raise Refusal(f"not a commit hash: {', '.join(bad)}")
    item: LineItem = {"label": label, "batch": str(Path(batch).resolve()) if batch else "", "commits": commits,
                      "added": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    with edited() as items:
        if any(entry["label"] == label for entry in items):
            raise Refusal(f"item {label!r} is already in the land line")
        insert(items, item, first=first, before=before)


def move(label: str, *, first: bool, before: str | None) -> None:
    with edited() as items:
        item = items.pop(position(items, label))
        insert(items, item, first=first, before=before)


def remove(label: str) -> None:
    with edited() as items:
        _ = items.pop(position(items, label))


def take_next() -> str:
    """Remove the first item and return its label; an empty line returns an empty label."""
    with edited() as items:
        return items.pop(0)["label"] if items else ""


def listing() -> str:
    items = read_items()
    if isinstance(items, NoLine) or not items:
        return "the land line is empty"
    rows: list[str] = []
    for number, item in enumerate(items, 1):
        named = [f"{entry.unit} {entry.phase} {entry.commit[:9]}" for entry in batch_commits(item["batch"])]
        named += [commit[:9] for commit in item["commits"]]
        rows.append(f"{number}. {item['label']}: {', '.join(named) or 'no commit'}")
    return "\n".join(rows)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Keep the line of checkpoint lands.")
    commands = root.add_subparsers(dest="command", required=True)
    added = commands.add_parser("add", help="add an item to the end of the line, or at a named place")
    _ = added.add_argument("label")
    _ = added.add_argument("--batch", default="")
    _ = added.add_argument("--commit", action="append")
    moved = commands.add_parser("move", help="move an item to a named place")
    _ = moved.add_argument("label")
    for command in (added, moved):
        where = command.add_mutually_exclusive_group(required=command is moved)
        _ = where.add_argument("--first", action="store_true")
        _ = where.add_argument("--before")
    removed = commands.add_parser("remove", help="remove an item")
    _ = removed.add_argument("label")
    _ = commands.add_parser("next", help="remove the first item and print its label")
    _ = commands.add_parser("list", help="print the line")
    return root


class Arguments(argparse.Namespace):
    command: str = ""
    label: str = ""
    batch: str = ""
    commit: list[str] | None = None
    first: bool = False
    before: str | None = None


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv, namespace=Arguments())
    try:
        match args.command:
            case "add":
                add(args.label, batch=args.batch, commits=args.commit or [], first=args.first, before=args.before)
            case "move":
                move(args.label, first=args.first, before=args.before)
            case "remove":
                remove(args.label)
            case "next":
                label = take_next()
                if label:
                    print(label)
            case _:
                print(listing())
    except (Refusal, OSError, ValueError, KeyError, TypeError) as error:
        print(f"land_queue: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

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
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Generator, Sequence
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
from typing import Literal, NamedTuple, NotRequired, TypedDict, cast
from uuid import uuid4

from scripts.lib import file_lock
from scripts.lib.atomic_file import write_text_atomic
from scripts.production import land_runs

HASH = re.compile(r"[0-9a-f]{7,40}")
SETTINGS_LABEL = "settings publish"
SETTINGS_WAIT_NOTICE = "main: waits for the land that runs now"


class SettingsGap(NamedTuple):
    """An open descriptor holds the gap until the publisher exits."""

    descriptor: int


class SettingsWaiting(NamedTuple):
    """A running land has the line. One item holds the next gap."""

    def report(self) -> str:
        return SETTINGS_WAIT_NOTICE


class SettingsGapBusy(NamedTuple):
    """Another process holds the gap lock or edits the line."""


class DirectSettingsPush(TypedDict):
    kind: Literal["direct"]
    checkout: str
    commit: str
    base: str
    branch: str
    validated_base: str
    settings_json_only: NotRequired[bool]
    journal_order: NotRequired[int]


class FastTrackSettingsPush(TypedDict):
    kind: Literal["fast-track"]
    attempt: str


type DeferredSettingsPush = DirectSettingsPush | FastTrackSettingsPush


def settings_only(paths: Sequence[str]) -> bool:
    def settings_path(name: str) -> bool:
        path = Path(name)
        return name == "settings.json" or (path.parent == Path("config") and path.suffix == ".conf")

    return bool(paths) and all(settings_path(name) for name in paths)


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

    @property
    def carrier_name(self) -> str:
        """Use the recorded production for a queue unit's carried work."""
        unit, separator, name = self.owner.partition(" ")
        if unit == "queue" and separator:
            production, labeled, _ = self.label.partition(" ")
            return f"{production} {name}" if labeled and production and production != "queue" else name
        return self.owner or self.label


class LandView(NamedTuple):
    """The lands that run now, the land line, and the newest result of each land.

    A land also takes each commit its listed commit holds that is not on main: carried_running and carried_line.
    """

    running: dict[str, str]
    line: dict[str, Place] | NoLine
    results: dict[str, land_runs.LandResult]
    carried_running: frozenset[str]
    carried_line: dict[str, Place]


class LandRunning(NamedTuple):
    pass


class InLine(NamedTuple):
    """The commit waits at a place; failed is the newest result when that try did not land.

    carried is True when a later commit at that place holds this commit.
    """

    place: Place
    failed: land_runs.LandResult | None
    carried: bool


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


def _lock_path(name: str) -> Path:
    folder = line_path().parent
    folder.mkdir(parents=True, exist_ok=True)
    return folder / name


@contextmanager
def line_lock(*, wait: bool = True) -> Generator[file_lock.LockResult]:
    with file_lock.exclusive(_lock_path("land_queue.json.lock"), wait=wait) as result:
        yield result


def _gap_lock_path() -> Path:
    return _lock_path("settings-gap.lock")


@contextmanager
def gap_lock(*, wait: bool = True) -> Generator[SettingsGap | SettingsGapBusy]:
    with _gap_lock_path().open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            yield SettingsGapBusy()
        else:
            yield SettingsGap(lock.fileno())


def inherited_gap(descriptor: int) -> SettingsGap:
    if not os.path.samestat(os.fstat(descriptor), _gap_lock_path().stat()):
        raise OSError("the publisher did not receive the land gap")
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return SettingsGap(descriptor)


@contextmanager
def landing(commits: Sequence[str], owners: Sequence[str]) -> Generator[None]:
    """Register a land after the current settings push ends."""
    with ExitStack() as record:
        with gap_lock(), line_lock():
            record.enter_context(land_runs.landing(commits, owners))
        yield


def write_items(items: list[LineItem]) -> None:
    write_text_atomic(line_path(), json.dumps(items, indent=1) + "\n")


@contextmanager
def edited() -> Generator[list[LineItem]]:
    """Hold the line lock through the edit."""
    with line_lock():
        read = read_items()
        items = [] if isinstance(read, NoLine) else read
        yield items
        write_items(items)


def _settings_item(item: LineItem) -> bool:
    return item["label"] == SETTINGS_LABEL and not item["batch"] and not item["commits"]


class PublicationReceipt(TypedDict):
    commit: str
    state: Literal["pushed", "contained"]


def _publication_path(checkout: Path, commit: str) -> Path:
    key = hashlib.sha256(json.dumps([str(checkout.resolve()), commit]).encode()).hexdigest()
    return line_path().parent / "settings-publication" / f"{key}.json"


def publication_receipt(checkout: Path, commit: str) -> PublicationReceipt | None:
    try:
        return cast(PublicationReceipt, json.loads(_publication_path(checkout, commit).read_bytes()))
    except FileNotFoundError:
        return None


def record_publication(checkout: Path, requested: str, receipt: PublicationReceipt) -> None:
    path = _publication_path(checkout, requested)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, json.dumps(receipt) + "\n", new_file_mode=0o600)


def _spool(name: str) -> Path:
    path = line_path().parent / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def _request_path(request: DeferredSettingsPush) -> Path:
    if request["kind"] == "direct":
        identity = [request["kind"], request["checkout"], request["commit"]]
    else:
        identity = [request["kind"], request["attempt"]]
    token = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    return _spool("settings-journal") / f"{token}.json"


def deferred_pushes() -> list[DeferredSettingsPush]:
    entries: list[tuple[int, DeferredSettingsPush]] = []
    for path in _spool("settings-journal").glob("*.json"):
        try:
            with path.open("rb") as source:
                entries.append((os.fstat(source.fileno()).st_mtime_ns,
                                cast(DeferredSettingsPush, json.load(source))))
        except FileNotFoundError:
            continue
    return [request for _, request in sorted(entries, key=lambda entry: entry[0])]


def finish_push(request: DeferredSettingsPush) -> None:
    _request_path(request).unlink(missing_ok=True)


def _settings_row() -> LineItem:
    return {"label": SETTINGS_LABEL, "batch": "", "commits": [],
            "added": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def enqueue_settings(request: DeferredSettingsPush | None = None) -> SettingsWaiting:
    """Record a publication request without waiting for the line editor."""
    if request is not None:
        write_text_atomic(_request_path(request), json.dumps(request) + "\n", new_file_mode=0o600)
    with line_lock(wait=False) as line:
        if line is file_lock.LockResult.BUSY:
            write_text_atomic(_spool("settings-wake") / uuid4().hex, "")
        else:
            _mark_settings()
    return SettingsWaiting()


def _mark_settings() -> None:
    found = read_items()
    items = [] if isinstance(found, NoLine) else found
    if not any(_settings_item(item) for item in items):
        items.insert(0, _settings_row())
        write_items(items)


def _visible_items() -> list[LineItem] | NoLine:
    found = read_items()
    if not any((line_path().parent / "settings-wake").glob("*")):
        return found
    items = [] if isinstance(found, NoLine) else found
    return items if any(_settings_item(item) for item in items) else [_settings_row(), *items]


@contextmanager
def settings_gap(*, wait: bool = True) -> Generator[SettingsGap | SettingsWaiting | SettingsGapBusy]:
    """Reserve the next gap or hold the current gap without holding the line."""
    # Take the gap, line, main push, and journal locks in this order.
    # Release the line lock before Git runs.
    with gap_lock(wait=wait) as gap:
        if isinstance(gap, SettingsGapBusy):
            yield SettingsGapBusy()
            return
        with line_lock(wait=wait) as line:
            if line is file_lock.LockResult.BUSY:
                admission = SettingsGapBusy()
            elif land_runs.running_lands():
                _mark_settings()
                admission = SettingsWaiting()
            else:
                admission = gap
        yield admission


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
        items = _visible_items()
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
    # Load the board reader here. Publication only needs the line.
    carried = cast(Callable[[Path, str], list[str]],
                   import_module("scripts.production.waits_on").carried)
    running, line = land_runs.running_lands(), places()
    carried_running = frozenset(commit for listed in running for commit in carried(repo, listed))
    carried_line: dict[str, Place] = {}
    if not isinstance(line, NoLine):
        for listed, place in sorted(line.items(), key=lambda entry: entry[1].number):
            for commit in carried(repo, listed):
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
        return InLine(place, failed, False)
    if result is not None and result["landed"]:
        return LandEnded(result)
    if land_runs.holds(view.carried_running, commit):
        return LandRunning()
    if (carrier := place_of(view.carried_line, commit)) is not None:
        return InLine(carrier, failed, True)
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
    if _settings_item(item):
        index = 0
    elif items and _settings_item(items[0]):
        index = max(index, 1)
    items.insert(index, item)


def check_label(label: str) -> None:
    if not re.fullmatch(r"\S+ \S+", label):
        raise Refusal(f"label {label!r} must contain two words separated by one space")


def check_batch_path(batch: str) -> None:
    if any(character.isspace() for character in batch):
        raise Refusal(f"batch path {batch!r} must have no white space")


def add(label: str, *, batch: str, commits: list[str], first: bool, before: str | None) -> None:
    check_label(label)
    check_batch_path(batch)
    batch_path = str(Path(batch).resolve()) if batch else ""
    check_batch_path(batch_path)
    if batch_path and not batch_commits(batch_path):
        raise Refusal(f"batch file {batch} names no commit")
    if not batch_path and not commits:
        raise Refusal("give --batch or --commit")
    bad = [commit for commit in commits if not HASH.fullmatch(commit)]
    if bad:
        raise Refusal(f"not a commit hash: {', '.join(bad)}")
    item: LineItem = {"label": label, "batch": batch_path, "commits": commits,
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


def _take_land_item(items: list[LineItem]) -> str:
    if not items or _settings_item(items[0]):
        return ""
    label, batch = items[0]["label"], items[0]["batch"]
    check_label(label)
    check_batch_path(batch)
    _ = items.pop(0)
    return f"{label} {batch}" if batch else label


def take_next() -> str:
    """Claim the gap and consume the settings row before starting the publisher."""
    with gap_lock() as gap:
        assert isinstance(gap, SettingsGap)
        with edited() as items:
            wakes = list((line_path().parent / "settings-wake").glob("*"))
            marker = bool(items and _settings_item(items[0]))
            if not marker and not wakes:
                return _take_land_item(items)
            if land_runs.running_lands():
                return ""
            if marker:
                _ = items.pop(0)
            for wake in wakes:
                wake.unlink(missing_ok=True)
        _publish_claim(gap)
        with edited() as items:
            if items and _settings_item(items[0]):
                settings = items.pop(0)
                following = _take_land_item(items)
                items.insert(0, settings)
                return following
            return _take_land_item(items)


def _publish_claim(gap: SettingsGap) -> None:
    script = Path(__file__).resolve().parents[1] / "lib/live_settings.py"
    command = [sys.executable, str(script), "publish-queued", str(gap.descriptor)]
    try:
        result = subprocess.run(command, pass_fds=(gap.descriptor,), capture_output=True, text=True, check=False)
    except OSError as error:
        print(f"settings publication could not start: {error}", file=sys.stderr)
    else:
        for output in (result.stderr, result.stdout):
            if output:
                print(output, end="", file=sys.stderr)
        if result.returncode:
            print(f"settings publication stopped with exit {result.returncode}", file=sys.stderr)


def listing() -> str:
    items = _visible_items()
    if isinstance(items, NoLine) or not items:
        return "the land line is empty"
    rows: list[str] = []
    for number, item in enumerate(items, 1):
        if _settings_item(item):
            rows.append(f"{number}. {SETTINGS_LABEL}: waiting changes")
            continue
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
    _ = commands.add_parser("next", help="remove the first item and print its label and any batch path")
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
                output = take_next()
                if output:
                    print(output)
            case _:
                print(listing())
    except (Refusal, OSError, ValueError, KeyError, TypeError) as error:
        print(f"land_queue: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

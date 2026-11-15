#!/usr/bin/env python3
"""Store the work a unit director plans to do outside its plan."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import json
import os
import sys
from pathlib import Path
from typing import NamedTuple, TypedDict, cast

from scripts.lib import atomic_file, file_lock
from scripts.production import staff


def state_path(staff_id: staff.StaffId) -> Path:
    root = Path(os.environ.get("UNIT_QUEUE_STATE_DIR", "~/.local/state/unit-queue")).expanduser()
    return root / f"{staff_id}.json"


class QueuedItem(NamedTuple):
    """One queued item; after names the work it waits on, or is empty."""

    text: str
    after: str = ""


class StoredItem(TypedDict):
    text: str
    after: str


def read_items(staff_id: staff.StaffId) -> list[QueuedItem]:
    """Read the queue; an item is text, or an object with text and the work it waits on."""
    path = state_path(staff_id)
    try:
        value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{path}: expected a list of queued items")
    items: list[QueuedItem] = []
    for entry in cast(list[object], value):
        if isinstance(entry, dict):
            fields = cast(dict[str, object], entry)
            text, after = fields.get("text"), fields.get("after")
        else:
            text, after = entry, ""
        if not isinstance(text, str) or not text.strip() or not isinstance(after, str):
            raise ValueError(f"{path}: expected nonempty text for each queued item")
        items.append(QueuedItem(text, after))
    return items


def read(staff_id: staff.StaffId) -> list[str]:
    return [item.text for item in read_items(staff_id)]


def stored(item: QueuedItem) -> str | StoredItem:
    return {"text": item.text, "after": item.after} if item.after else item.text


def shown(item: QueuedItem) -> str:
    return f"{item.text} (after {item.after})" if item.after else item.text


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add")
    _ = add.add_argument("--text", required=True)
    _ = add.add_argument("--after", default="",
                         help="the work this item waits on: a commit, a branch, or <unit>/<phase>")
    for command in ("done", "clear", "show"):
        _ = commands.add_parser(command)
    args = parser.parse_args(arguments)
    command = cast(str, args.command)
    try:
        row = staff.local_snapshot().by_current_session(staff.caller_identity())
        if row is None or row.kind != "unit":
            raise ValueError("this session has no unit director staff row")
        path = state_path(row.id)
        if command == "show":
            items = read_items(row.id)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            with file_lock.exclusive(path.with_suffix(".lock")):
                items = [] if command == "clear" else read_items(row.id)
                if command == "add":
                    text = " ".join(cast(str, args.text).split())
                    after = cast(str, args.after).strip()
                    if not text:
                        raise ValueError("the queued item must contain text")
                    if len(after.split()) > 1:
                        raise ValueError("--after names one commit, branch, or <unit>/<phase>")
                    items.append(QueuedItem(text, after))
                elif command == "done":
                    items = items[1:]
                else:
                    items = []
                try:
                    atomic_file.write_text_atomic(path, json.dumps([stored(item) for item in items], ensure_ascii=False) + "\n")
                except atomic_file.AtomicDurabilityUnconfirmed as error:
                    print(f"unit_queue: warning: {error}", file=sys.stderr)
        print("\n".join(f"{index}. {shown(item)}" for index, item in enumerate(items, 1)) or "—")
        return 0
    except (OSError, ValueError) as error:
        print(f"unit_queue: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

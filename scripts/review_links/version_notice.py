"""Record the List items in a new console version.

Tabs open before the first deployment need one reload.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
from pathlib import Path
import re
import subprocess
from typing import cast

from scripts.lib import atomic_file, file_lock
from scripts.production import the_list

PATH = "/console/version-notice"
FILE = ".review-link-version.json"
LIST_ITEM = re.compile(r"\bList\s+([1-9][0-9]*)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Change:
    number: int
    key: str
    title: str
    cleared: bool = False


@dataclass(frozen=True)
class Notice:
    revision: str = ""
    changes: tuple[Change, ...] = ()

    def payload(self, token: str) -> dict[str, object]:
        return {"revision": self.revision, "token": token,
                "changes": [asdict(change) for change in self.changes if not change.cleared]}


class VersionChanged(ValueError):
    """The request refers to an earlier console version."""


@dataclass(frozen=True)
class ClearOne:
    number: int


class ClearAll:
    """Clear every change in the version."""


def git(checkout: Path, *arguments: str) -> str:
    return subprocess.run(["git", "-C", str(checkout), *arguments], check=True, timeout=15,
                          capture_output=True, encoding="utf-8", errors="replace").stdout


def list_numbers(messages: str) -> tuple[int, ...]:
    return tuple(dict.fromkeys(int(match[1]) for match in LIST_ITEM.finditer(messages)))


def read(root: Path) -> Notice:
    try:
        raw = (root / FILE).read_text(encoding="utf-8")
    except FileNotFoundError:
        return Notice()
    try:
        data = cast(dict[str, object], json.loads(raw))
        rows = cast(list[dict[str, object]], data["changes"])
        return Notice(cast(str, data["revision"]), tuple(Change(
            cast(int, row["number"]), cast(str, row["key"]), cast(str, row["title"]),
            cast(bool, row["cleared"])) for row in rows))
    except (KeyError, TypeError) as error:
        raise ValueError("Invalid console version record.") from error


def _write(root: Path, notice: Notice) -> None:
    atomic_file.write_text_atomic(root / FILE, json.dumps(asdict(notice), ensure_ascii=False), new_file_mode=0o600)


def start(root: Path, checkout: Path) -> None:
    """Record HEAD and scan its new commits once at server start."""
    revision = git(checkout, "rev-parse", "--verify", "HEAD").strip()
    with file_lock.exclusive(root / (FILE + ".lock")):
        previous = read(root)
        if previous.revision == revision:
            return
        changes: dict[int, Change] = {}
        if previous.revision:
            try:
                messages = git(checkout, "log", "--format=%B%x00", f"{previous.revision}..{revision}")
            except subprocess.CalledProcessError:
                # Git cannot read the range. Use HEAD as the new baseline.
                messages = ""
            numbers = list_numbers(messages)
            if numbers:
                rows = {row.item.number: row for row in the_list.display_list_records(include_closed=True)}
                for number in numbers:
                    if (row := rows.get(number)) is not None:
                        changes[number] = Change(number, f"{row.keeper}/{row.item.id}", row.item.title)
        for change in previous.changes:
            if not change.cleared:
                _ = changes.setdefault(change.number, change)
        _write(root, Notice(revision, tuple(changes.values())))


def clear(root: Path, revision: str, scope: ClearOne | ClearAll) -> Notice:
    """Clear one line or all lines in the version the user saw."""
    with file_lock.exclusive(root / (FILE + ".lock")):
        current = read(root)
        if revision != current.revision:
            raise VersionChanged("The version changed. Read the new changes before you clear them.")
        changes = tuple(
            replace(change, cleared=True) if isinstance(scope, ClearAll) or change.number == scope.number else change
            for change in current.changes
        )
        if changes == current.changes:
            return current
        updated = replace(current, changes=changes)
        _write(root, updated)
        return updated

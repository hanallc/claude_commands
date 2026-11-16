"""Store investigations by fixed staff owner and record their answers."""
from __future__ import annotations

try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import fcntl
import json
import os
import re
import sys
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import cast

from scripts.lib.atomic_file import write_text_atomic
from scripts.production import staff
from scripts.production.issues import source_paths

IDENTITY = re.compile(r"I[1-9][0-9]*")
ANSWER_SECTION = "\n\n## Answer\n\n"


class InvestigationError(ValueError):
    """An investigation cannot accept the requested operation."""


class InvestigationState(StrEnum):
    OPEN = "open"
    ANSWER_READY = "answer ready"
    READ = "read"


@dataclass(frozen=True)
class Investigation:
    id: str
    owner: staff.StaffId
    title: str
    state: InvestigationState
    filed: str
    answered: str
    read: str
    question: str
    answer: str


def state_root() -> Path:
    """Return the root for all owners' investigation records."""
    return Path(os.environ.get("SHOWRUNNER_STATE_DIR") or Path.home() / ".local/state/showrunner")


def folder(owner: staff.StaffId) -> Path:
    """Return the owner's investigation folder without creating it."""
    if not owner or Path(owner).name != owner or owner in (".", ".."):
        raise InvestigationError("invalid owner staff id")
    return state_root() / owner / "investigations"


def _path(owner: staff.StaffId, identity: str) -> Path:
    if IDENTITY.fullmatch(identity) is None:
        raise InvestigationError("id must be I followed by a positive number")
    return folder(owner) / f"{identity}.md"


def load(owner: staff.StaffId, identity: str, *, sources: list[Path] | None = None) -> Investigation:
    """Read one complete investigation for command and console callers."""
    path = _path(owner, identity)
    if sources is not None:
        sources.append(path)
    try:
        with path.open(encoding="utf-8", newline="") as stream:
            text = stream.read()
    except FileNotFoundError:
        raise InvestigationError(f"{identity}: no investigation for owner {owner}") from None
    except UnicodeDecodeError as error:
        raise InvestigationError(f"{identity}: invalid investigation: {error}") from None
    try:
        if not text.startswith("---\n"):
            raise ValueError("missing frontmatter")
        header, body = text[4:].split("\n---\n\n", 1)
        fields: dict[str, str] = {}
        for line in header.splitlines():
            key, value = line.split(": ", 1)
            decoded = cast(object, json.loads(value))
            if not isinstance(decoded, str) or key in fields:
                raise ValueError("invalid field")
            fields[key] = decoded
        length = int(fields["question_length"])
        if length < 0 or body[length:length + len(ANSWER_SECTION)] != ANSWER_SECTION:
            raise ValueError("missing Answer section")
        item = Investigation(fields["id"], staff.StaffId(fields["owner"]), fields["title"],
                             InvestigationState(fields["state"]), fields["filed"], fields["answered"],
                             fields["read"], body[:length], body[length + len(ANSWER_SECTION):])
        if item.id != identity or item.owner != owner:
            raise ValueError("record identity differs from its path")
        if not item.title.strip() or item.title.splitlines() != [item.title]:
            raise ValueError("title must have one line")
        for name, value, required in (
                ("filed", item.filed, True),
                ("answered", item.answered, item.state != InvestigationState.OPEN),
                ("read", item.read, item.state == InvestigationState.READ)):
            if required:
                if datetime.fromisoformat(value).utcoffset() is None:
                    raise ValueError(f"{name} must have a time zone")
            elif value:
                raise ValueError(f"{name} must be empty in state {item.state}")
        return item
    except (ValueError, KeyError) as error:
        raise InvestigationError(f"{identity}: invalid investigation: {error}") from None


def records(owner: staff.StaffId, *, include_read: bool = False,
            sources: list[Path] | None = None) -> list[Investigation]:
    """Read investigations in numeric order and watch the folder and each record."""
    paths = source_paths(folder(owner), "I*.md", sources=sources)
    items: list[Investigation] = []
    for path in paths:
        if sources is not None:
            sources.append(path)
        try:
            items.append(load(owner, path.stem))
        except InvestigationError as error:
            print(f"investigation: {path}: {str(error).replace(chr(10), ' ')}", file=sys.stderr)
    return sorted((item for item in items if include_read or item.state != InvestigationState.READ),
                  key=lambda item: int(item.id[1:]))


@contextmanager
def _locked(owner: staff.StaffId) -> Generator[None]:
    directory = folder(owner)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _save(item: Investigation) -> None:
    fields = {"id": item.id, "owner": item.owner, "title": item.title, "state": item.state,
              "filed": item.filed, "answered": item.answered, "read": item.read,
              "question_length": str(len(item.question))}
    header = "\n".join(f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in fields.items())
    write_text_atomic(_path(item.owner, item.id), f"---\n{header}\n---\n\n{item.question}{ANSWER_SECTION}{item.answer}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def file(owner: staff.StaffId, title: str, question: str) -> Investigation:
    """File an investigation with the next number for this owner."""
    if not title.strip() or title.splitlines() != [title]:
        raise InvestigationError("title must have one nonempty line")
    if not question.strip():
        raise InvestigationError("question must not be empty")
    with _locked(owner):
        paths = source_paths(folder(owner), "I*.md")
        number = max((int(path.stem[1:]) for path in paths if IDENTITY.fullmatch(path.stem)), default=0) + 1
        item = Investigation(f"I{number}", owner, title, InvestigationState.OPEN, _now(), "", "", question, "")
        _save(item)
        return item


def answer(owner: staff.StaffId, identity: str, text: str) -> Investigation:
    """Save an answer once; accept an exact repeat without writing."""
    if not text.strip():
        raise InvestigationError("answer must not be empty")
    _ = _path(owner, identity)
    with _locked(owner):
        item = load(owner, identity)
        if item.state == InvestigationState.ANSWER_READY and item.answer == text:
            return item
        if item.state != InvestigationState.OPEN:
            raise InvestigationError(f"{identity}: cannot answer an investigation in state {item.state}")
        item = replace(item, state=InvestigationState.ANSWER_READY, answered=_now(), answer=text)
        _save(item)
        return item


def mark_read(owner: staff.StaffId, identity: str) -> Investigation:
    """Mark an answer read once; accept an exact repeat without writing."""
    _ = _path(owner, identity)
    with _locked(owner):
        item = load(owner, identity)
        if item.state == InvestigationState.READ:
            return item
        if item.state != InvestigationState.ANSWER_READY:
            raise InvestigationError(f"{identity}: cannot read an investigation in state {item.state}")
        item = replace(item, state=InvestigationState.READ, read=_now())
        _save(item)
        return item


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    create = subcommands.add_parser("file")
    _ = create.add_argument("--title", required=True)
    _ = create.add_argument("--question", required=True)
    respond = subcommands.add_parser("answer")
    _ = respond.add_argument("--id", required=True)
    _ = respond.add_argument("--text", required=True)
    _ = respond.add_argument("--owner", default="")
    read = subcommands.add_parser("read")
    _ = read.add_argument("--id", required=True)
    _ = read.add_argument("--owner", default="")
    listing = subcommands.add_parser("list")
    _ = listing.add_argument("--all", action="store_true")
    args = parser.parse_args(argv)
    try:
        record = staff.snapshot()
        caller = record.by_current_session(staff.caller_identity())
        if caller is None:
            raise InvestigationError("the current session has no staff row")
        command = cast(str, args.command)
        owner = caller.id
        if command in ("answer", "read") and (reference := cast(str, args.owner)):
            target = record.by_id(staff.StaffId(reference))
            if target is None:
                raise InvestigationError("owner has no staff row")
            owner = target.id
        if command == "file":
            print(file(owner, cast(str, args.title), cast(str, args.question)).id)
        elif command == "answer":
            _ = answer(owner, cast(str, args.id), cast(str, args.text))
        elif command == "read":
            _ = mark_read(owner, cast(str, args.id))
        else:
            for item in records(owner, include_read=cast(bool, args.all)):
                print(f"{item.id} | {item.state} | {item.title}")
        return 0
    except (InvestigationError, staff.StaffRefused, OSError, UnicodeError) as error:
        print(f"investigation: {str(error).replace(chr(10), ' ')}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

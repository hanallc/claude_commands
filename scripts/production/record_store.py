"""Read and atomically write numbered Markdown records under a staff owner."""
from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple, cast

from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.file_lock import exclusive
from scripts.production import staff
from scripts.production.issues import source_paths


class RecordText(NamedTuple):
    fields: dict[str, str]
    body: str


def state_root() -> Path:
    """Return the shared root for records owned by staff."""
    return Path(os.environ.get("SHOWRUNNER_STATE_DIR") or Path.home() / ".local/state/showrunner")


def section(name: str) -> str:
    return f"\n\n## {name}\n\n"


def split_section(body: str, length: int, name: str) -> tuple[str, str]:
    """Split exact text without treating a heading inside it as a boundary."""
    marker = section(name)
    if length < 0 or body[length:length + len(marker)] != marker:
        raise ValueError(f"missing {name} section")
    return body[:length], body[length + len(marker):]


@dataclass(frozen=True)
class NumberedRecords:
    owner: staff.StaffId
    collection: str
    prefix: str
    noun: str
    error_type: type[ValueError]

    def valid_id(self, identity: str) -> bool:
        return re.fullmatch(re.escape(self.prefix) + r"[1-9][0-9]*", identity) is not None

    def folder(self) -> Path:
        if not self.owner or Path(self.owner).name != self.owner or self.owner in (".", ".."):
            raise self.error_type("invalid owner staff id")
        return state_root() / self.owner / self.collection

    def path(self, identity: str) -> Path:
        if not self.valid_id(identity):
            raise self.error_type(f"id must be {self.prefix} followed by a positive number")
        return self.folder() / f"{identity}.md"

    @contextmanager
    def locked(self) -> Generator[None]:
        directory = self.folder()
        directory.mkdir(parents=True, exist_ok=True)
        with exclusive(directory / ".lock"):
            yield

    def next_number(self) -> int:
        paths = source_paths(self.folder(), f"{self.prefix}*.md")
        return max((int(path.stem[len(self.prefix):]) for path in paths if self.valid_id(path.stem)), default=0) + 1

    def invalid(self, identity: str, error: object) -> ValueError:
        return self.error_type(f"{identity}: invalid {self.noun}: {error}")

    def read(self, identity: str, *, sources: list[Path] | None = None) -> RecordText:
        path = self.path(identity)
        if sources is not None:
            sources.append(path)
        try:
            with path.open(encoding="utf-8", newline="") as stream:
                text = stream.read()
        except FileNotFoundError:
            raise self.error_type(f"{identity}: no {self.noun} for owner {self.owner}") from None
        except UnicodeDecodeError as error:
            raise self.invalid(identity, error) from None
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
            if fields["id"] != identity:
                raise ValueError("record identity differs from its path")
            return RecordText(fields, body)
        except (ValueError, KeyError) as error:
            raise self.invalid(identity, error) from None

    def write(self, identity: str, fields: Mapping[str, str], body: str) -> None:
        header = "\n".join(f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in fields.items())
        write_text_atomic(self.path(identity), f"---\n{header}\n---\n\n{body}")

    def records[T](self, load: Callable[[str], T], *, sources: list[Path] | None = None) -> list[T]:
        items: list[T] = []
        for path in source_paths(self.folder(), f"{self.prefix}*.md", sources=sources):
            if sources is not None:
                sources.append(path)
            try:
                items.append(load(path.stem))
            except self.error_type as error:
                print(f"{self.noun}: {path}: {str(error).replace(chr(10), ' ')}", file=sys.stderr)
        return items

#!/usr/bin/env python3
"""Read the frontmatter of the issues in a hanadocs issues folder.

An issue is one `*.md` directly in the folder; its research notes live in `<folder>/<slug>/` and
are not issues. Its frontmatter sits between the leading `---` lines, one `key: value` per line,
a list either inline as `[a, b]` or as `- item` lines below its key. Obsidian writes a link as
`"[[target]]"` or `"[[target|alias]]"`; a value reads as the alias, else the target.
"""
from __future__ import annotations

import fnmatch
import json
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from io import StringIO
import re
from pathlib import Path
from typing import NamedTuple, cast

ALL_FIELDS: frozenset[str] = frozenset()
FENCE = "---"
# The tag every proposal issue carries.
PROPOSAL_TAG = "proposal"
FIELD = re.compile(r"^(?P<key>[A-Za-z_][\w-]*):\s*(?P<value>.*)$")
ITEM = re.compile(r"^\s+-\s*(?P<value>.*)$")
WIKI_LINK = re.compile(r"^\[\[(?P<target>[^\]|]*)(?:\|(?P<alias>[^\]]*))?\]\]$")


class Issue(NamedTuple):
    path: Path
    # Every field as a list: a plain value is a list of one, an empty one a list of none.
    fields: dict[str, list[str]]

    def value(self, key: str) -> str:
        """The field's first value, or nothing when it is empty or missing."""
        return next(iter(self.fields.get(key, [])), "")


@dataclass(frozen=True)
class IssueFieldValue:
    """A field value and the start of its unchanged source text."""

    text: str = field(repr=False)
    start: int


def _plain_value(raw: str, start: int) -> IssueFieldValue:
    text = raw.lstrip()
    start += len(raw) - len(text)
    text = text.rstrip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        inner = text[1:-1].lstrip()
        start += len(text) - len(inner) - 1
        text = inner.rstrip()
    if link := WIKI_LINK.match(text):
        part = "alias" if link["alias"] else "target"
        value = link[part].lstrip()
        start += link.start(part) + len(link[part]) - len(value)
        text = value.rstrip()
    return IssueFieldValue(text, start)


def plain(value: str) -> str:
    """A value without its quotes and the brackets of a wiki link."""
    return _plain_value(value, 0).text


def _field_values(raw: str, start: int) -> list[IssueFieldValue]:
    text = raw.lstrip()
    start += len(raw) - len(text)
    text = text.rstrip()
    if text.startswith("[") and not text.startswith("[["):
        items = text.lstrip("[]")
        start += len(text) - len(items)
        values: list[IssueFieldValue] = []
        for item in items.rstrip("[]").split(","):
            value = _plain_value(item, start)
            if value.text:
                values.append(value)
            start += len(item) + 1
        return values
    value = _plain_value(text, start)
    return [value] if value.text else []


def values(raw: str) -> list[str]:
    """The values one `key: value` line holds: an inline list's items, or the one plain value."""
    return [value.text for value in _field_values(raw, 0)]


@dataclass(frozen=True)
class IssueField:
    """One field declaration and its values, including continuation lines."""

    values: list[IssueFieldValue] = field(default_factory=list)


def located_header_fields(lines: Iterable[str], keys: frozenset[str] = ALL_FIELDS, *,
                          start: int = 0) -> dict[str, list[IssueField]]:
    """Read declarations in order and keep the source position of each value."""
    fields: dict[str, list[IssueField]] = {}
    active = IssueField()
    selected = False
    for source in lines:
        line = source.rstrip("\r\n")
        if (item := ITEM.match(line)) and selected:
            active.values.extend(_field_values(item["value"], start + item.start("value")))
        elif found := FIELD.match(line):
            key = found["key"]
            selected = not keys or key in keys
            if selected:
                active = IssueField(_field_values(found["value"], start + found.start("value")))
                fields.setdefault(key, []).append(active)
        start += len(source)
    return fields


def decoded_header_fields(fields: dict[str, list[IssueField]]) -> dict[str, list[str]]:
    """Use the last declaration of each field."""
    return {key: [value.text for value in declarations[-1].values] for key, declarations in fields.items()}


def header_fields(lines: list[str], keys: frozenset[str] = ALL_FIELDS) -> dict[str, list[str]]:
    """Decode only the requested fields from one header."""
    return decoded_header_fields(located_header_fields(lines, keys))


@dataclass(frozen=True)
class IssueHeaderSource:
    """The exact lines of a leading header, with its fences."""

    opening: str = ""
    lines: tuple[str, ...] = field(default=(), repr=False)
    closing: str = ""

    def located_fields(self, keys: frozenset[str] = ALL_FIELDS) -> dict[str, list[IssueField]]:
        return located_header_fields(self.lines, keys, start=len(self.opening))

    def decoded_fields(self, keys: frozenset[str] = ALL_FIELDS) -> dict[str, list[str]]:
        return decoded_header_fields(self.located_fields(keys)) if self.closing else {}


def read_issue_header(stream: Iterator[str]) -> IssueHeaderSource:
    """Read through the closing fence. Leave the body unread."""
    return _read_header(stream, lambda line: line.strip() == FENCE)


def read_numbered_header(stream: Iterator[str]) -> IssueHeaderSource:
    """Read the header of a numbered record from a stream that splits at LF."""
    return _read_header(stream, lambda line: line.rstrip("\r\n") == FENCE)


def _read_header(stream: Iterator[str], is_fence: Callable[[str], bool]) -> IssueHeaderSource:
    opening = next(stream, "")
    if not is_fence(opening):
        return IssueHeaderSource()
    lines: list[str] = []
    for line in stream:
        if is_fence(line):
            return IssueHeaderSource(opening, tuple(lines), line)
        lines.append(line)
    return IssueHeaderSource(opening, tuple(lines))


def issue_text_header(text: str) -> IssueHeaderSource:
    return read_issue_header(StringIO(text, newline=""))


@dataclass(frozen=True)
class NumberedFieldValue:
    """A decoded numbered field and its encoded source position."""

    text: str = field(repr=False)
    encoded: str = field(repr=False)
    start: int


@dataclass(frozen=True)
class LocatedNumberedNote:
    fields: dict[str, NumberedFieldValue]
    body: str = field(repr=False)

    def decoded_fields(self) -> dict[str, str]:
        return {key: value.text for key, value in self.fields.items()}


def decode_numbered_field(value: str) -> str:
    decoded = cast(object, json.loads(value))
    if not isinstance(decoded, str):
        raise ValueError("invalid field")
    return decoded


def encoded_numbered_header_values(header: IssueHeaderSource, keys: frozenset[str]) -> dict[str, list[str]]:
    """Find encoded values in selected numbered fields before validating the record."""
    fields: dict[str, list[str]] = {}
    for line in header.lines:
        key, separator, value = line.partition(": ")
        if separator and key in keys:
            fields.setdefault(key, []).append(value)
    return fields


class DuplicateNumberedField(ValueError):
    """A numbered record declares the same header field twice."""


def numbered_header_fields(header: IssueHeaderSource) -> dict[str, NumberedFieldValue]:
    """Decode numbered header strings and keep their source positions."""
    if header.opening.rstrip("\r\n") != FENCE or header.closing.rstrip("\r\n") != FENCE:
        raise ValueError("missing frontmatter")
    fields: dict[str, NumberedFieldValue] = {}
    offset = len(header.opening)
    for source in header.lines:
        key, separator, value = source.rstrip("\r\n").partition(": ")
        if not separator:
            raise ValueError("invalid field")
        if key in fields:
            raise DuplicateNumberedField("duplicate numbered field")
        fields[key] = NumberedFieldValue(decode_numbered_field(value), value, offset + len(key) + len(separator))
        offset += len(source)
    return fields


def located_numbered_note(text: str) -> LocatedNumberedNote:
    """Read a numbered record without changing its line endings."""
    stream = StringIO(text, newline="\n")
    fields = numbered_header_fields(read_numbered_header(stream))
    if stream.readline() not in ("\n", "\r\n"):
        raise ValueError("missing blank line after frontmatter")
    return LocatedNumberedNote(fields, stream.read())


def frontmatter(path: Path, *, sources: list[Path] | None = None,
                keys: frozenset[str] = ALL_FIELDS,
                select: Callable[[Issue], bool] | None = None) -> dict[str, list[str]]:
    """Select from cheap header fields before decoding the complete header."""
    if sources is not None:
        sources.append(path)
    try:
        with path.open(encoding="utf-8", newline="") as stream:
            header = read_issue_header(stream)
            if header.closing:
                fields = header.decoded_fields(keys)
                if select is None:
                    return fields
                return header.decoded_fields() if select(Issue(path, fields)) else {}
    except (OSError, UnicodeError):
        if sources is not None:
            raise
        return {}
    if sources is not None:
        raise ValueError(f"{path}: missing or unclosed frontmatter")
    return {}


def source_paths(folder: Path, pattern: str, *, sources: list[Path] | None = None) -> list[Path]:
    """List source paths. A collector requires readable folders; missing folders are empty."""
    if sources is None:
        return list(folder.glob(pattern))
    sources.append(folder)
    try:
        # fnmatch on the name: Path.match costs about 15 µs a path, and an issues folder holds 1600.
        return sorted(path for path in folder.iterdir() if fnmatch.fnmatchcase(path.name, pattern))
    except FileNotFoundError:
        return []


def open_issues(folder: Path, tag: str, *, sources: list[Path] | None = None,
                select: Callable[[Issue], bool] | None = None, keys: frozenset[str] = ALL_FIELDS) -> list[Issue]:
    """The issues directly in the folder whose status is open and whose tags hold the tag, by name."""
    def wanted(issue: Issue) -> bool:
        return (issue.value("status") == "open" and tag in issue.fields.get("tags", [])
                and (select is None or select(issue)))

    found: list[Issue] = []
    for path in sorted(source_paths(folder, "*.md", sources=sources)):
        if not path.is_file():
            continue
        fields = frontmatter(path, sources=sources, keys=keys | {"status", "tags"}, select=wanted)
        if fields:
            found.append(Issue(path, fields))
    return found

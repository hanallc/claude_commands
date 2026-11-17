#!/usr/bin/env python3
"""Read the frontmatter of the issues in a hanadocs issues folder.

An issue is one `*.md` directly in the folder; its research notes live in `<folder>/<slug>/` and
are not issues. Its frontmatter sits between the leading `---` lines, one `key: value` per line,
a list either inline as `[a, b]` or as `- item` lines below its key. Obsidian writes a link as
`"[[target]]"` or `"[[target|alias]]"`; a value reads as the alias, else the target.
"""
from __future__ import annotations

import fnmatch
from collections.abc import Callable
import re
from pathlib import Path
from typing import NamedTuple

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


def plain(value: str) -> str:
    """A value without its quotes and the brackets of a wiki link."""
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    link = WIKI_LINK.match(text)
    return (link["alias"] or link["target"]).strip() if link else text


def values(raw: str) -> list[str]:
    """The values one `key: value` line holds: an inline list's items, or the one plain value."""
    text = raw.strip()
    if text.startswith("[") and not text.startswith("[["):
        items = [plain(item) for item in text.strip("[]").split(",")]
        return [item for item in items if item]
    one = plain(text)
    return [one] if one else []


def header_fields(lines: list[str], keys: frozenset[str] = ALL_FIELDS) -> dict[str, list[str]]:
    """Decode only the requested fields from one header."""
    fields: dict[str, list[str]] = {}
    key = ""
    for line in lines:
        if (item := ITEM.match(line)) and key:
            fields[key].extend(values(item["value"]))
        elif found := FIELD.match(line):
            key = found["key"] if not keys or found["key"] in keys else ""
            if key:
                fields[key] = values(found["value"])
    return fields


def frontmatter(path: Path, *, sources: list[Path] | None = None,
                keys: frozenset[str] = ALL_FIELDS,
                select: Callable[[Issue], bool] | None = None) -> dict[str, list[str]]:
    """Select from cheap header fields before decoding the complete header."""
    if sources is not None:
        sources.append(path)
    try:
        with path.open(encoding="utf-8") as stream:
            if stream.readline().strip() == FENCE:
                lines: list[str] = []
                for line in stream:
                    if line.strip() == FENCE:
                        fields = header_fields(lines, keys)
                        if select is None:
                            return fields
                        return header_fields(lines) if select(Issue(path, fields)) else {}
                    lines.append(line)
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

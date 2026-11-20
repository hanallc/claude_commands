"""Parse and replay the rows of the model registry text; no files and no git."""
from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from typing import NamedTuple


class RegistryRow(NamedTuple):
    section: str
    key: str
    value: str
    line: str


class RowChange(NamedTuple):
    section: str
    key: str
    replacement: str  # An empty string deletes the row.


def _indexed_records(lines: Iterable[str]) -> Iterator[tuple[int, RegistryRow]]:
    section = ""
    for index, line in enumerate(lines):
        content = line.partition("#")[0].strip()
        if not content:
            continue
        if content.startswith("[") and content.endswith("]"):
            section = content[1:-1]
            yield index, RegistryRow(section, "", "", line)
        else:
            key, separator, value = content.partition("=")
            yield index, RegistryRow(section, key.strip(), value.strip() if separator else "", line)


def records(text: str) -> list[RegistryRow]:
    return [row for _, row in _indexed_records(text.splitlines())]


def parse_registry(text: str) -> dict[str, dict[str, str]]:
    """Parse sections and rows from registry text, including empty sections."""
    sections: dict[str, dict[str, str]] = {}
    for row in records(text):
        section = sections.setdefault(row.section, {})
        if row.key:
            section[row.key] = row.value
    return sections


def rows(text: str) -> dict[tuple[str, str], RegistryRow]:
    return {(row.section, row.key): row for row in records(text)
            if row.key and "=" in row.line.partition("#")[0]}


def replay_pending(head: str, local: str, target: str) -> str:
    """Put the rows that local changed from head onto target."""
    return replay(target, changes(head, local), False)


def changes(before: str, after: str) -> list[RowChange]:
    previous, wanted = rows(before), rows(after)
    return [RowChange(section, key, wanted[section, key].line if (section, key) in wanted else "")
            for section, key in dict.fromkeys([*previous, *wanted])
            if previous.get((section, key), RegistryRow(section, key, "", "")).value
            != wanted.get((section, key), RegistryRow(section, key, "", "")).value
            or ((section, key) in previous) != ((section, key) in wanted)]


def values(text: str) -> dict[tuple[str, str], str]:
    return {address: row.value for address, row in rows(text).items()}


def catalog_text(before: str, after: str) -> str:
    catalog = [change for change in changes(before, after)
               if change.section in ("codex.agents", "codex.families")]
    return replay(before, catalog, True)


def version(model: str) -> tuple[int, ...]:
    match = re.match(r"gpt-([0-9]+(?:\.[0-9]+)*)-", model)
    return tuple(int(part) for part in match[1].split(".")) if match else ()


def additive_line(change: RowChange, current: dict[tuple[str, str], RegistryRow]) -> str:
    """Union efforts and retain the newest family target across machine catalogs."""
    prior = current.get((change.section, change.key))
    if not change.replacement:
        return prior.line if prior else ""
    value = change.replacement.partition("#")[0].split("=", 1)[1].strip()
    if prior and change.section == "codex.agents":
        efforts = dict.fromkeys(item.strip() for item in f"{prior.value},{value}".split(",") if item.strip())
        value = ",".join(efforts)
    elif prior and change.section == "codex.families" and version(prior.value) >= version(value):
        return prior.line
    return f"{change.key}={value}"


def replay(text: str, edits: list[RowChange], additive_catalog: bool) -> str:
    source = text.splitlines()
    current: dict[tuple[str, str], RegistryRow] = {}
    addresses: dict[int, tuple[str, str]] = {}
    insert_after = {"": -1}
    for index, row in _indexed_records(source):
        if row.key and "=" in row.line.partition("#")[0]:
            address = row.section, row.key
            current[address] = row
            addresses[index] = address
        elif not row.key and row.line.lstrip().startswith("["):
            insert_after[row.section] = index
    replacements = {(change.section, change.key):
                    additive_line(change, current) if additive_catalog and change.section in (
                        "codex.agents", "codex.families") else change.replacement
                    for change in edits}

    for index, address in addresses.items():
        source[index] = replacements.get(address, source[index])
        if source[index]:
            insert_after[address[0]] = index

    new_rows: dict[int, list[str]] = {}
    new_sections: dict[str, list[str]] = {}
    for address, line in replacements.items():
        if address in current or not line:
            continue
        section = address[0]
        if section in insert_after:
            new_rows.setdefault(insert_after[section], []).append(line)
        else:
            new_sections.setdefault(section, []).append(line)

    lines = new_rows.get(-1, []).copy()
    for index, line in enumerate(source):
        if line or index not in addresses:
            lines.append(line)
        lines.extend(new_rows.get(index, ()))
    for section, section_rows in new_sections.items():
        lines.extend(["", f"[{section}]", *section_rows])
    return "\n".join(lines) + "\n"

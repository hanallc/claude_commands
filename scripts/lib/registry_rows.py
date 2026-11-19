"""Parse and replay the rows of the model registry text; no files and no git."""
from __future__ import annotations

import re
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


def records(text: str) -> list[RegistryRow]:
    found: list[RegistryRow] = []
    section = ""
    for line in text.splitlines():
        content = line.partition("#")[0].strip()
        if not content:
            continue
        if content.startswith("[") and content.endswith("]"):
            section = content[1:-1]
            found.append(RegistryRow(section, "", "", line))
        else:
            key, separator, value = content.partition("=")
            found.append(RegistryRow(section, key.strip(), value.strip() if separator else "", line))
    return found


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
    current = rows(text)
    replacements = {(change.section, change.key):
                    additive_line(change, current) if additive_catalog and change.section in (
                        "codex.agents", "codex.families") else change.replacement
                    for change in edits}
    lines: list[str] = []
    section = ""
    insert_at = 0

    def insert_new_rows() -> None:
        new_rows: list[str] = []
        for address in list(replacements):
            if address[0] == section:
                line = replacements.pop(address)
                if line:
                    new_rows.append(line)
        lines[insert_at:insert_at] = new_rows

    for line in text.splitlines():
        content = line.partition("#")[0].strip()
        if content.startswith("[") and content.endswith("]"):
            insert_new_rows()
            section = content[1:-1]
            insert_at = len(lines) + 1
        elif "=" in content:
            key = content.split("=", 1)[0].strip()
            line = replacements.pop((section, key), line)
            if not line:
                continue
            if key:
                insert_at = len(lines) + 1
        lines.append(line)
    insert_new_rows()
    for address, line in replacements.items():
        new_section = address[0]
        if not line:
            continue
        if section != new_section:
            lines.extend(["", f"[{new_section}]"])
            section = new_section
        lines.append(line)
    return "\n".join(lines) + "\n"

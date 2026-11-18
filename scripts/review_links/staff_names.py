"""Render staff names with the current Accounts role colors."""
from __future__ import annotations

import html
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from string import Formatter
from typing import Literal, TypedDict

from scripts.accounts import accounts
from scripts.production import staff
from scripts.lib.build_steps import StepNames
from scripts.message.owner_role import OwnerRole


@dataclass(frozen=True)
class StaffReference:
    """Mark a staff reference before the caller joins it with other text."""

    name: str


class StaffNamePart(TypedDict):
    kind: Literal["staff"]
    text: str


class ContextTextPart(TypedDict):
    kind: Literal["text"]
    text: str


NamePart = StaffNamePart | ContextTextPart


def text_parts(*values: str | StaffReference) -> list[NamePart]:
    """Keep staff references separate from their context text."""
    parts: list[NamePart] = []
    for value in values:
        text = value.name if isinstance(value, StaffReference) else value
        if not text:
            continue
        if isinstance(value, StaffReference):
            parts.append({"kind": "staff", "text": text})
        elif parts and parts[-1]["kind"] == "text":
            parts[-1]["text"] += text
        else:
            parts.append({"kind": "text", "text": text})
    return parts


def parts_text(parts: Sequence[NamePart]) -> str:
    """Keep the original text for plain output and old data readers."""
    return "".join(part["text"] for part in parts)


def template_parts(template: str, fields: Mapping[str, str | int | StaffReference]) -> list[NamePart]:
    """Mark template fields at their source without reading names from the result."""
    values: list[str | StaffReference] = []
    for literal, field, spec, conversion in Formatter().parse(template):
        values.append(literal)
        if field is not None:
            value = fields[field]
            text = value.name if isinstance(value, StaffReference) else value
            pattern = "{value" + ("!" + conversion if conversion else "") + (":" + spec if spec else "") + "}"
            shown = pattern.format(value=text)
            values.append(StaffReference(shown) if isinstance(value, StaffReference) else shown)
    return text_parts(*values)


def owned_step_parts(names: StepNames, step: str, unit: str, role: OwnerRole, *,
                     ci: bool = False, current: Sequence[NamePart] = ()) -> list[NamePart]:
    """Keep staff fields separate in the configured step name."""
    prefix = "owned.ci-" if ci else "owned.showrunner-" if role is OwnerRole.SHOWRUNNER else "owned."
    template = names.settings.get(prefix + step, names.settings.get(prefix + "default"))
    if template is not None and (ci or role is OwnerRole.SHOWRUNNER or role is OwnerRole.UNIT and unit):
        return template_parts(template, {"unit": unit if ci else StaffReference(unit), "step": names.shown(step)})
    return list(current) if current else text_parts(names.shown(step))


class NameReading(Enum):
    """The caller needs a current role color reading."""

    CURRENT = "current"


def for_page(names: StaffNames | NameReading, members: staff.StaffSnapshot) -> StaffNames:
    return names if isinstance(names, StaffNames) else StaffNames(members)


class StaffNames:
    """One staff snapshot and one color reading for a page render."""

    def __init__(self, members: staff.StaffSnapshot) -> None:
        self.members: staff.StaffSnapshot = members
        self.colors: dict[str, str] = accounts.role_colors()
        candidates: dict[str, list[staff.StaffRow]] = {}
        for member in members.rows:
            parent = members.parent_of(member)
            aliases = (member.id, member.current_session, member.name, member.name + " (" + member.kind + ")",
                       parent.name + "/" + member.name if parent is not None else member.name)
            for alias in set(aliases):
                if alias:
                    candidates.setdefault(alias, []).append(member)
        self.rows: dict[str, staff.StaffRow] = {alias: rows[0] for alias, rows in candidates.items() if len(rows) == 1}

    def style(self, reference: str) -> str:
        """Return a color attribute only for an unambiguous staff reference."""
        member = self.rows.get(reference)
        if member is None:
            return ""
        color = accounts.staff_role_color(member, self.colors)
        return f' style="color:{color.css}"' if isinstance(color, accounts.StaffRoleColor) else ""

    def html(self, reference: str, text: str = "") -> str:
        """Render one staff name, or escape a name that has no staff record."""
        label = html.escape(text or reference)
        member = self.rows.get(reference)
        if member is None:
            return label
        color = accounts.staff_role_color(member, self.colors)
        if not isinstance(color, accounts.StaffRoleColor):
            return label
        return f'<span class="staff-name" data-color-text="{color.key}" style="color:{color.css}">{label}</span>'

    def source(self, identity: staff.StaffId) -> str:
        """Keep a unit's parent and name separate, with their own role colors."""
        member = self.members.by_id(identity)
        if member is None:
            return html.escape(identity)
        parent = self.members.parent_of(member) if member.kind == "unit" else None
        lead = self.html(parent.id, parent.name) + " · " if parent is not None else ""
        return lead + self.html(member.id, member.name)

    def registry(self) -> dict[str, str]:
        """Supply the same escaped name spans to pages that render in JavaScript."""
        return {alias: self.html(member.id, alias) for alias, member in self.rows.items()}


def read() -> StaffNames:
    """Read current staff and role colors for a page without a staff snapshot."""
    try:
        members = staff.snapshot()
    except staff.StaffUnreadable:
        members = staff.StaffSnapshot(())
    return StaffNames(members)


def script(registry: dict[str, str]) -> str:
    """Share the Python name spans with a page's JavaScript renderer."""
    return '<script>' + SCRIPT.replace('STAFF_NAMES_DATA', json.dumps(registry).replace('<', '\\u003c')) + '</script>'


SCRIPT = r'''window.staffNames={
 values:STAFF_NAMES_DATA,
 set(values){this.values=values;},
 html(text){return this.values[text] ?? String(text).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#x27;'}[c]));},
 parts(text){return String(text).split(' · ').map(part=>this.html(part)).join(' · ');}
};'''

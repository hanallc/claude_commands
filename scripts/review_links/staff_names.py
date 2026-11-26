"""Render staff names and role labels with the current Accounts role colors."""
from __future__ import annotations

import html
import json
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Literal, TypedDict

from scripts.accounts import accounts
from scripts.build_queue.snapshot_codec import public_queue_text, row_text, select_queue_name
from scripts.production import staff


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


ROLE_GROUPS = {
    "producer": "producers",
    "showrunner": "showrunners",
    "unit": "units",
    "unit director": "units",
    "merge director": "units",
    "style director": "units",
    "worker": "workers",
}


class RoleLabel(TypedDict):
    opening: str
    css: str


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


def row_parts(owner: str, what: str, unit: str) -> list[NamePart]:
    """The parts of `<owner>: <what>`; an owner that is the staff unit is a staff reference."""
    text = row_text(owner, what)
    shown_owner = select_queue_name(owner)
    owner_part = StaffReference(shown_owner) if shown_owner == public_queue_text(unit) else shown_owner
    return text_parts(owner_part, text.removeprefix(shown_owner))


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
        return _role_span_open(color) + label + "</span>"

    def label_color(self, role: str) -> accounts.StaffRoleColor | accounts.NoAccountColor:
        """Resolve the configured color for an explicit role label."""
        group = ROLE_GROUPS.get(role)
        return accounts.NoAccountColor() if group is None else accounts.role_color(group, self.colors)

    def role_label(self, role: str, text: str) -> str:
        """Render a role label with escaped text."""
        label = html.escape(text)
        color = self.label_color(role)
        return _role_span_open(color) + label + "</span>" if isinstance(color, accounts.StaffRoleColor) else label

    def role_labels(self) -> dict[str, RoleLabel]:
        """Supply validated label colors and spans to JavaScript."""
        return {role: {"opening": _role_span_open(color), "css": color.css}
                for role in ROLE_GROUPS if isinstance(color := self.label_color(role), accounts.StaffRoleColor)}

    def source(self, identity: staff.StaffId) -> str:
        """Keep a unit's parent and name separate, with their own role colors."""
        member = self.members.by_id(identity)
        if member is None:
            return html.escape(identity)
        parent = self.members.parent_of(member) if member.kind == "unit" else None
        lead = self.html(parent.id, parent.name) + " · " if parent is not None else ""
        return lead + self.html(member.id, member.name)

    def unit_open(self) -> str:
        """Open the span of a unit name in the unit role color, or return "" when the setting has no valid color.

        A page shows a unit in the unit color with this span also when the unit has no staff entry.
        """
        color = accounts.role_color("units", self.colors)
        return _role_span_open(color) if isinstance(color, accounts.StaffRoleColor) else ""

    def registry(self) -> dict[str, str]:
        """Supply the same escaped name spans to pages that render in JavaScript."""
        return {alias: self.html(member.id, alias) for alias, member in self.rows.items()}


def _role_span_open(color: accounts.StaffRoleColor) -> str:
    """Open a span in the role color."""
    return f'<span class="staff-name" data-color-text="{color.key}" style="color:{color.css}">'


def read() -> StaffNames:
    """Read current staff and role colors for a page without a staff snapshot."""
    try:
        members = staff.snapshot()
    except staff.StaffUnreadable:
        members = staff.StaffSnapshot(())
    return StaffNames(members)


def script(registry: dict[str, str], role_labels: dict[str, RoleLabel] | None = None) -> str:
    """Share the name spans and role labels with JavaScript."""
    values = json.dumps({"values": registry, "roleLabels": role_labels or {}}).replace('<', '\\u003c')
    return '<script>' + SCRIPT.replace('STAFF_NAMES_DATA', values) + '</script>'


SCRIPT = r'''window.staffNames={
 ...STAFF_NAMES_DATA,
 unitOpen:'',
 set(values,unitOpen='',roleLabels=this.roleLabels){this.values=values;this.unitOpen=unitOpen;this.roleLabels=roleLabels;},
 escape(text){return String(text).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#x27;'}[c]));},
 html(text,reference=text){
  const value=this.values[reference];
  if(typeof value!=='string') return this.escape(text);
  if(text===reference) return value;
  const label=this.escape(text);
  return value.startsWith('<span ') ? value.slice(0,value.indexOf('>')+1)+label+'</span>' : label;
 },
 roleLabel(role,text){
  const opening=this.roleLabels[role]?.opening;
  const label=this.escape(text);
  return opening ? opening+label+'</span>' : label;
 },
 unit(text){return this.unitOpen ? this.unitOpen+this.escape(text)+'</span>' : this.html(text);},
 parts(text){return String(text).split(' · ').map(part=>this.html(part)).join(' · ');}
};'''

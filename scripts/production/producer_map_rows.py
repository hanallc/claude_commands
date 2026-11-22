"""Check a producer_map row against the staff record and name it for the comment above the row."""
from __future__ import annotations

from scripts.lib import conf
from scripts.lib.waiting_rows import NAMED_ROWS
from scripts.production import staff

MAPPED_KINDS: tuple[staff.Kind, ...] = ("showrunner", "unit")


def _name(identity: str, members: staff.StaffSnapshot) -> str:
    row = members.by_id(staff.StaffId(identity))
    return row.name if row is not None else identity


def _comment(key: str, value: str, members: staff.StaffSnapshot) -> str:
    return f"# {_name(key, members)} -> {_name(value, members)}"


def name_comment(key: str, value: str) -> str:
    """Name the member and its producer from the staff record now; an unknown id stands for itself."""
    return _comment(key, value, staff.snapshot())


def checked_comment(key: str, value: str) -> str:
    """Refuse a row unless its key is an active showrunner or unit id and its value an active producer id."""
    members = staff.snapshot()
    if key not in {row.id for kind in MAPPED_KINDS for row in staff.active(members, kind)}:
        raise conf.Unusable(f"{NAMED_ROWS}: {key}: not the staff id of an active showrunner or unit")
    if value not in {row.id for row in staff.active(members, "producer")}:
        raise conf.Unusable(f"{NAMED_ROWS}: {key}={value}: not the staff id of an active producer")
    return _comment(key, value, members)

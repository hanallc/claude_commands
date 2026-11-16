"""Read each build slot's size, last use and holder for the Disks page."""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, TypedDict, cast

from scripts.buildlog.store import host_name
from scripts.disks import measure
from scripts.disks.measure import DisksData
from scripts.lib import build_folders, build_slots, conf
from scripts.lib.build_steps import StepNames, read_step_names
from scripts.message.unit_labels import OwnerRole

State = Callable[[Path], Literal["held", "free"]]
# The shown name of a lease owner: a staff id or a role name.
OwnerNames = Callable[[str], str]


def staff_names() -> OwnerNames:
    """Name each staff id from this machine's staff record; keep any other owner text."""
    from scripts.production import staff

    try:
        rows = staff.local_snapshot()
    except staff.StaffUnreadable:
        return lambda owner: owner

    def name(owner: str) -> str:
        row = rows.by_id(staff.StaffId(owner))
        return row.name if row is not None else owner
    return name


class SlotUse(TypedDict):
    # "<unit> <step>", named as the Builds page names a step.
    name: str
    showrunner: str


class SlotRow(TypedDict):
    name: str
    number: int
    size: int | None
    last_used: float | None
    used_by: SlotUse | None
    deleted_tonight: bool


class SlotGroup(TypedDict):
    repository: str
    maximum: int | None
    slots: list[SlotRow]


class SlotReport(TypedDict):
    root: str
    groups: list[SlotGroup]


def lease_record(path: Path) -> dict[str, object]:
    try:
        record = cast(object, json.loads(build_slots.lease_path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}
    return cast(dict[str, object], record) if isinstance(record, dict) else {}


def last_use(record: Mapping[str, object]) -> datetime | None:
    value = record.get("last_use")
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def slot_use(record: Mapping[str, object], steps: StepNames, owners: OwnerNames) -> SlotUse:
    """Name the work in a lease from its owner and its step key, as the Builds page does."""
    text = {key: value for key in ("work", "purpose", "unit", "showrunner") if isinstance(value := record.get(key), str)}
    unit = owners(text.get("unit", ""))
    step = text.get("work", "")
    current = " ".join(part for part in (unit, text.get("purpose", "")) if part)
    name = steps.for_owner(step, unit, OwnerRole.UNIT, current_name=current) if step else current
    return {"name": name, "showrunner": text.get("showrunner", "")}


def slot_row(path: Path, number: int, cargo_root: Path, settings: conf.ConfigRead, data: DisksData | None, now: float,
             state: State, steps: StepNames, owners: OwnerNames) -> SlotRow:
    record = lease_record(path)
    held = state(path) == "held"
    used = last_use(record)
    sizes = [measure.tree_size(data, path), measure.tree_size(data, cargo_root / path.name)]
    known = [size for size in sizes if size is not None]
    return {"name": path.name, "number": number, "size": sum(known) if known else None,
            "last_used": now if held else used.timestamp() if used else None,
            "used_by": slot_use(record, steps, owners) if held else None,
            "deleted_tonight": (not held and used is not None
                                and build_slots.unused(used, settings, datetime.fromtimestamp(now, timezone.utc)))}


def read_slots(root: Path, cargo_root: Path, settings: conf.ConfigRead, machine: str, data: DisksData | None,
               now: float, state: State = build_folders.build_folder_state, owners: OwnerNames | None = None) -> SlotReport:
    """Each slot under root, grouped by repository, as the nightly sweep finds them."""
    groups: dict[str, list[SlotRow]] = {}
    steps = read_step_names()
    names = owners if owners is not None else staff_names()
    for path, repository, number in build_slots.slot_paths(root) if root.is_dir() else []:
        groups.setdefault(repository, []).append(slot_row(path, number, cargo_root, settings, data, now, state, steps, names))

    def maximum(repository: str) -> int | None:
        try:
            return build_slots.configured_maximum(settings, machine, repository)
        except conf.Unusable:
            return None

    return {"root": str(root), "groups": [{"repository": repository, "maximum": maximum(repository),
                                           "slots": sorted(rows, key=lambda row: row["number"])}
                                          for repository, rows in sorted(groups.items())]}


def slot_report(now: float) -> SlotReport | None:
    """The slots on this machine, or None when build_folders.conf names no slot root for it."""
    settings = conf.read_or_repository("build_folders")
    machine = host_name()
    try:
        root = build_slots.configured_slot_root()
    except build_slots.SlotsNotConfigured:
        return None
    return read_slots(root, build_slots.slot_cargo_root(settings), settings, machine, measure.read_data(), now)

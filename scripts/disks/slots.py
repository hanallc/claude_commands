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

State = Callable[[Path], Literal["held", "free"]]


class SlotUse(TypedDict):
    purpose: str
    unit: str
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


def slot_row(path: Path, number: int, cargo_root: Path, settings: conf.ConfigRead, data: DisksData | None, now: float,
             state: State) -> SlotRow:
    record = lease_record(path)
    held = state(path) == "held"
    used = last_use(record)
    sizes = [measure.tree_size(data, path), measure.tree_size(data, cargo_root / path.name)]
    known = [size for size in sizes if size is not None]
    text = {key: value for key in ("purpose", "unit", "showrunner") if isinstance(value := record.get(key), str)}
    return {"name": path.name, "number": number, "size": sum(known) if known else None,
            "last_used": now if held else used.timestamp() if used else None,
            "used_by": {"purpose": text.get("purpose", ""), "unit": text.get("unit", ""),
                        "showrunner": text.get("showrunner", "")} if held else None,
            "deleted_tonight": (not held and used is not None
                                and build_slots.unused(used, settings, datetime.fromtimestamp(now, timezone.utc)))}


def read_slots(root: Path, cargo_root: Path, settings: conf.ConfigRead, machine: str, data: DisksData | None,
               now: float, state: State = build_folders.build_folder_state) -> SlotReport:
    """Each slot under root, grouped by repository, as the nightly sweep finds them."""
    groups: dict[str, list[SlotRow]] = {}
    for path, repository, number in build_slots.slot_paths(root) if root.is_dir() else []:
        groups.setdefault(repository, []).append(slot_row(path, number, cargo_root, settings, data, now, state))

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
        root = build_slots.slot_folder_root(settings, machine)
    except build_slots.SlotsNotConfigured:
        return None
    return read_slots(root, build_slots.slot_cargo_root(settings), settings, machine, measure.read_data(), now)

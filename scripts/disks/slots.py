"""Read each build slot's size, last use and holder for the Disks page."""
from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path, PurePath
from typing import Literal, TypedDict, cast

from scripts.buildlog.store import host_name
from scripts.disks import disks_page
from scripts.disks.measure import DisksData, Folder
from scripts.lib import build_folders, conf

SLOT_NAME = re.compile(r"(.+)-slot([1-9][0-9]*)")
LEASE_SUFFIX = ".lease.json"
DAY_SECONDS = 86400

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


def tree_size(data: DisksData | None, path: PurePath) -> int | None:
    """A path's measured size: 0 when its drive lists no such folder, None when no measured drive holds it."""
    drives = sorted(data["drives"] if data else [], key=lambda drive: len(drive["mount"]), reverse=True)
    for drive in drives:
        mount = PurePath(drive["mount"])
        if path != mount and mount not in path.parents:
            continue
        node: Folder | None = drive.get("tree")
        if node is None:
            return None
        parts = path.relative_to(mount).parts
        while parts:
            # A chain of single children is one node, such as "home/natepiano".
            match = next((child for child in node["children"]
                          if PurePath(child["name"]).parts[:len(parts)] == parts[:len(PurePath(child["name"]).parts)]), None)
            if match is None:
                return 0
            node, parts = match, parts[len(PurePath(match["name"]).parts):]
        return node["size"]
    return None


def lease_record(path: Path) -> dict[str, object]:
    try:
        record = cast(object, json.loads(path.with_name(path.name + LEASE_SUFFIX).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}
    return cast(dict[str, object], record) if isinstance(record, dict) else {}


def last_use(record: Mapping[str, object]) -> float | None:
    value = record.get("last_use")
    try:
        return datetime.fromisoformat(value).timestamp() if isinstance(value, str) else None
    except ValueError:
        return None


def slot_row(path: Path, number: int, cargo_root: Path, data: DisksData | None, now: float, unused_days: int,
             state: State) -> SlotRow:
    record = lease_record(path)
    held = state(path) == "held"
    used = last_use(record)
    sizes = [tree_size(data, path), tree_size(data, cargo_root / path.name)]
    known = [size for size in sizes if size is not None]
    text = {key: value for key in ("purpose", "unit", "showrunner") if isinstance(value := record.get(key), str)}
    return {"name": path.name, "number": number, "size": sum(known) if known else None,
            "last_used": now if held else used,
            "used_by": {"purpose": text.get("purpose", ""), "unit": text.get("unit", ""),
                        "showrunner": text.get("showrunner", "")} if held else None,
            # The nightly sweep deletes a free slot whose last use is older than slots.unused_days.
            "deleted_tonight": not held and used is not None and now - used > unused_days * DAY_SECONDS}


def read_slots(root: Path, cargo_root: Path, values: Mapping[str, str], machine: str, data: DisksData | None,
               now: float, state: State = build_folders.build_folder_state) -> SlotReport:
    """Each slot under root, grouped by repository, as the nightly sweep finds them."""
    unused_days = int(values["slots.unused_days"])
    names: set[str] = {entry.name.removesuffix(LEASE_SUFFIX) for entry in root.iterdir()} if root.is_dir() else set()
    groups: dict[str, list[SlotRow]] = {}
    for name in names:
        if (match := SLOT_NAME.fullmatch(name)) is not None:
            groups.setdefault(match[1], []).append(
                slot_row(root / name, int(match[2]), cargo_root, data, now, unused_days, state))

    def maximum(repository: str) -> int | None:
        value = values.get(f"slots.{machine}.{repository}.max", values.get(f"slots.{machine}.default_max"))
        return int(value) if value else None

    return {"root": str(root), "groups": [{"repository": repository, "maximum": maximum(repository),
                                           "slots": sorted(rows, key=lambda row: row["number"])}
                                          for repository, rows in sorted(groups.items())]}


def config_path(value: str) -> Path:
    return Path(os.path.expandvars(value)).expanduser().resolve()


def slot_report(now: float) -> SlotReport | None:
    """The slots on this machine, or None when build_folders.conf names no slot root for it."""
    values = conf.read("build_folders").values()
    machine = host_name()
    root = values.get(f"roots.slots.{machine}", values.get("roots.slots"))
    cargo_root = values.get(f"roots.slot_cargo.{machine}", values.get("roots.slot_cargo"))
    if not root or not cargo_root:
        return None
    try:
        data = cast(DisksData, json.loads((disks_page.output_folder() / disks_page.DATA_FILE).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        data = None
    return read_slots(config_path(root), config_path(cargo_root), values, machine, data, now)

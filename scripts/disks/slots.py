"""Read each build slot's size, last use and holder for the Disks page."""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Literal, NotRequired, TypedDict, cast

from scripts.buildlog.store import host_name
from scripts.disks import measure
from scripts.disks.measure import DisksData
from scripts.lib import build_folders, conf
from scripts.lib.build_steps import StepNames, read_step_names
from scripts.build_queue.snapshot_codec import row_text
from scripts.review_links import staff_names as name_rendering

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
    name_parts: NotRequired[list[name_rendering.NamePart]]


class SlotRow(TypedDict):
    name: str
    key: NotRequired[str]
    number: int
    size: int | None
    last_used: float | None
    used_by: SlotUse | None
    needs_review: bool


class SlotGroup(TypedDict):
    repository: str
    maximum: int | None
    slots: list[SlotRow]


class SlotReport(TypedDict):
    root: str
    # The machine of the slots; a Builds page row names the slot folder and machine it runs in.
    machine: str
    # The nightly sweep lists a free copy after this many days without use.
    unused_days: int
    groups: list[SlotGroup]


def lease_record(path: Path) -> dict[str, object]:
    try:
        record = cast(object, json.loads(build_folders.lease_path(path).read_text(encoding="utf-8")))
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
    what = steps.shown(step) if step else text.get("purpose", "")
    return {"name": row_text(unit, what), "showrunner": text.get("showrunner", ""),
            "name_parts": name_rendering.row_parts(unit, what, unit)}


def slot_row(path: Path, form: build_folders.BuildCopy, days: int, data: DisksData | None, now: float,
             state: State, steps: StepNames, owners: OwnerNames) -> SlotRow:
    record = lease_record(path)
    try:
        held = state(path) == "held"
    except (OSError, ValueError):
        held = True
        record = {}
    used = last_use(record)
    try:
        stamp = (path / build_folders.COPY_USE_STAMP).lstat().st_mtime
    except OSError:
        stamp = None
    return {"name": path.name, "key": f"{form.repo}/{form.unit}/{form.number}", "number": form.number, "size": measure.tree_size(data, path),
            "last_used": now if held else used.timestamp() if used else stamp,
            "used_by": slot_use(record, steps, owners) if held else None,
            "needs_review": not held and stamp is not None and stamp <= now - days * 86400}


def read_slots(root: Path, settings: conf.ConfigRead, machine: str, data: DisksData | None,
               now: float, state: State = build_folders.build_folder_state, owners: OwnerNames | None = None) -> SlotReport:
    """Group numbered copies by repository and unit for the Disks page."""
    groups: dict[tuple[str, str], list[SlotRow]] = {}
    steps = read_step_names()
    names = owners if owners is not None else staff_names()
    days = settings.whole("folders.unused_days")
    for path, form in build_folders.copy_folders(root):
        groups.setdefault((form.repo, form.unit), []).append(slot_row(path, form, days, data, now, state, steps, names))
    return {"root": str(root), "machine": machine, "unused_days": days, "groups": [
        {"repository": f"{repo}/{unit}", "maximum": None, "slots": sorted(rows, key=lambda row: row["number"])}
        for (repo, unit), rows in sorted(groups.items())
    ]}


def slot_report(now: float) -> SlotReport:
    """Read the numbered copies on this machine."""
    return read_slots(build_folders.build_root(), conf.read_or_repository("build_folders"), host_name(),
                      measure.read_data(), now)

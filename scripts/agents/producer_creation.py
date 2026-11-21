"""Keep the shared marker for a local producer creation record."""
from __future__ import annotations

import json
import os
import re
import shlex
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from collections.abc import Generator, Mapping
from pathlib import Path
from typing import cast
from urllib.parse import quote

from scripts.agents import pane_record, remote
from scripts.lib import atomic_file, models_registry, registry_rows
from scripts.lib import names as display_names
from scripts.production import staff

SECTION = "producer_creations"


def record_path(name: str) -> Path:
    return pane_record.record_directory() / f".producer-add-{quote(name, safe='')}"


def read_record(path: Path) -> dict[str, object]:
    try:
        raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return {}
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"{path}: invalid producer creation record")
    return cast(dict[str, object], raw)


def write_record(path: Path, record: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_file.write_text_atomic(path, json.dumps(record, sort_keys=True) + "\n")


def _completed_lookup(name: str, row: staff.StaffRow) -> dict[str, object]:
    saved = read_record(record_path(name))
    if not saved:
        return {}
    if (saved.get("staff", row.id) != row.id
            or "staff" not in saved and not isinstance(saved.get("opening_confirmed"), str)):
        raise ValueError(f"{name}: the creation record is not complete for this producer")
    return {**saved, "staff": row.id}


def _receipt_path(row: staff.StaffRow) -> Path:
    return pane_record.record_directory() / f".producer-created-{row.id}"


def plan_completed_record(name: str, row: staff.StaffRow) -> dict[str, object]:
    """Record the creation evidence that the rename must preserve."""
    saved = _completed_lookup(name, row)
    receipt = read_record(_receipt_path(row))
    if receipt and (receipt.get("staff") != row.id or saved and receipt != saved):
        raise ValueError(f"{name}: completed creation evidence differs")
    return saved or receipt


def retain_completed_record(name: str, row: staff.StaffRow, expected: dict[str, object]) -> None:
    """Verify the planned receipt before clearing the creation lookup."""
    path = record_path(name)
    receipt_path = _receipt_path(row)
    saved = _completed_lookup(name, row)
    receipt = read_record(receipt_path)
    if (saved or receipt) != expected or (receipt and receipt != expected):
        raise ValueError(f"{name}: completed creation evidence differs")
    if saved:
        if not receipt:
            write_record(receipt_path, expected)
        else:
            atomic_file.sync_folder(receipt_path.parent)
        path.unlink()
    if path.parent.exists():
        atomic_file.sync_folder(path.parent)


@contextmanager
def operation(name: str) -> Generator[None]:
    with staff.slot_operation("producer-lifecycle", name), staff.slot_operation("producer-first-" + name, name):
        yield


def recovery_command(*arguments: str) -> str:
    loaded = display_names.runtime_names()
    if isinstance(loaded, display_names.Names):
        return shlex.join([loaded.audience("agents"), *arguments])
    scripts = Path(__file__).resolve().parents[1]
    return shlex.join([str(scripts / "lib/py"), str(scripts / "agents/shutdown.py"), *arguments])


def retry(name: str) -> str:
    return recovery_command("add-producer", name)


@dataclass(frozen=True)
class Marker:
    machine: str
    creation_id: str

    def value(self) -> str:
        return f"{self.machine}:{self.creation_id}"

    @classmethod
    def parse(cls, value: str) -> Marker:
        machine, _, creation_id = value.partition(":")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", machine) or not re.fullmatch(r"[0-9a-f]{32}", creation_id):
            raise ValueError("invalid producer creation marker")
        return cls(machine, creation_id)


def marker_for(record: Mapping[str, object]) -> Marker:
    creation_id = record.get("creation_id")
    if not isinstance(creation_id, str):
        raise ValueError("the creation record has no creation ID")
    return Marker.parse(f"{remote.this_machine()}:{creation_id}")


def require_record(name: str, record: Mapping[str, object]) -> Marker | None:
    creation = staff.producer_creation_registry("read", name)
    if isinstance(creation, staff.ProducerCreationRegistryStatus):
        return None
    marker = Marker.parse(creation.marker)
    if marker.machine != remote.this_machine() or record.get("creation_id") != marker.creation_id:
        raise ValueError(f"the creation record is on {marker.machine}. Run {retry(creation.name)} there.")
    return marker


def claim(name: str, marker: Marker, profile: models_registry.ProducerProfile) -> None:
    _ = staff.producer_creation_registry("claim", name, marker.value(), profile.value())


def saved_profile(record: Mapping[str, object]) -> models_registry.ProducerProfile | None:
    if not record:
        return None
    profile = record.get("profile")
    if not isinstance(profile, str):
        raise ValueError("the creation record has no profile")
    return models_registry.parse_producer_profile(profile)


def publish_profile(row: staff.StaffRow, profile: models_registry.ProducerProfile) -> None:
    """Publish the saved profile before the pane binds to the staff row."""
    _ = models_registry.store_producer(row.id, *profile, preserve_existing=True)
    _ = os.environ.pop(models_registry.OPERATION_ENVIRONMENT[0], None)


def finish(name: str, marker: Marker) -> None:
    _ = staff.producer_creation_registry("finish", name, marker.value())


def owner_registry_action(action: str, name: str, value: str,
                          profile: str) -> staff.RecordedProducerCreation | staff.ProducerCreationRegistryStatus:
    """Compare and edit the marker on the staff owner under one lock."""
    staff.validate_name("producer", name)
    if any(mark in name for mark in "#[]="):
        raise ValueError("invalid producer creation key")
    key = name.casefold()
    with staff.slot_operation("producer-marker-" + key, name):
        path = models_registry.registry_path()
        before = models_registry.registry_text(path)
        registry = models_registry.parse_registry(before)
        markers = registry.get(SECTION, {})
        marker_name = next((stored for stored in markers if stored.casefold() == key), name)
        current = markers.get(marker_name)
        if action == "read":
            return (staff.RecordedProducerCreation(marker_name, current) if current
                    else staff.ProducerCreationRegistryStatus.ABSENT)
        marker = Marker.parse(value)
        if current and current != value:
            owner = Marker.parse(current)
            raise ValueError(f"the creation record is on {owner.machine}. Run {retry(marker_name)} there.")
        if action == "claim":
            if current is not None:
                raise ValueError(f"{name}: the producer already has a creation marker")
            with suppress(staff.StaffAbsent):
                if any(row.name.casefold() == key for row in staff.authority_snapshot().all_rows):
                    raise ValueError(f"{name}: a staff row already has this name")
            _ = models_registry.parse_producer_profile(profile)
            edits = [registry_rows.RowChange(SECTION, name, f"{name}={marker.value()}")]
        elif action == "finish":
            edits = [registry_rows.RowChange(SECTION, marker_name, "")]
        else:
            raise ValueError("unknown producer creation action")
        after = registry_rows.replay(before, edits, False)
        _ = models_registry.store_registry(path, before, after, f"{action} producer creation for {name}")
        return staff.ProducerCreationRegistryStatus.UPDATED

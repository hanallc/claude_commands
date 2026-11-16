"""Read open waits in a phase instance for the Units report."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import NamedTuple, cast


class NamedWaitHolder(NamedTuple):
    """The holder named in a wait record."""

    label: str


class UnnamedWaitHolder(NamedTuple):
    """The wait record names no holder."""


class PhaseWait(NamedTuple):
    """An open worker or director wait in one phase instance."""

    reason: str
    started: float
    holder: NamedWaitHolder | UnnamedWaitHolder
    wait_kind: str
    resource_kind: str
    resource: str
    pass_instance_id: str
    owner_session: str
    wait_attribution: Mapping[str, object]


class UnitWait(NamedTuple):
    """An open unit wait on named work in one run."""

    target: str
    by: str
    reason: str
    kind: str
    started: float
    wait_id: str


class NoUnitWait(NamedTuple):
    """The work has no recorded unit wait."""


NO_UNIT_WAIT = NoUnitWait()


def unit_waits(events: Sequence[Mapping[str, object]]) -> list[UnitWait]:
    """Read open unit waits in start order, with the newest reason per target."""
    waits: dict[str, UnitWait] = {}
    for event in events:
        kind = event.get("event_type")
        if kind == "run_finished":
            waits.clear()
        elif kind == "phase_started":
            _ = waits.pop(str(event.get("phase_id", "")), None)
        elif kind in ("wait_finished", "wait_unclosed"):
            wait_id = event.get("wait_id")
            waits = {target: wait for target, wait in waits.items() if wait.wait_id != wait_id}
        elif kind == "wait_started" and event.get("wait_scope") == "unit":
            target = str(event.get("target_work", ""))
            started = event.get("wait_started_epoch", event.get("timestamp_epoch"))
            if target and isinstance(started, int | float) and not isinstance(started, bool):
                _ = waits.pop(target, None)
                waits[target] = UnitWait(target, str(event.get("wait_by", "")),
                                         str(event.get("wait_reason", "")), str(event.get("wait_kind", "")),
                                         float(started), str(event.get("wait_id", "")))
    return list(waits.values())


def _wait_holder(value: object) -> NamedWaitHolder | UnnamedWaitHolder:
    if isinstance(value, dict):
        fields = cast(dict[str, object], value)
        holder = fields.get("holder")
        if fields.get("state") == "NamedHolder" and isinstance(holder, dict):
            holder = cast(dict[str, object], holder)
            label = holder.get("unit") or holder.get("session_id")
            if isinstance(label, str) and label:
                return NamedWaitHolder(label)
    return UnnamedWaitHolder()


def phase_waits(events: Sequence[Mapping[str, object]], instance_id: str) -> list[PhaseWait]:
    """Read waits that remain open in one phase instance."""
    waits: dict[str, Mapping[str, object]] = {}
    for event in events:
        kind = event.get("event_type")
        wait_id = str(event.get("wait_id", ""))
        if kind == "run_finished" or kind == "phase_finished" and event.get("phase_instance_id") == instance_id:
            waits.clear()
        elif (kind == "wait_started" and event.get("wait_scope") != "unit"
              and event.get("phase_instance_id") == instance_id):
            waits[wait_id] = event
        elif kind in ("wait_finished", "wait_unclosed"):
            _ = waits.pop(wait_id, None)
        elif kind == "wait_holder_seen" and wait_id in waits:
            waits[wait_id] = {**waits[wait_id], "holder": event.get("holder")}
        elif kind == "pass_finished":
            waits = {key: wait for key, wait in waits.items()
                     if not event.get("pass_instance_id") or wait.get("pass_instance_id") != event.get("pass_instance_id")}
    result: list[PhaseWait] = []
    for wait in waits.values():
        started = wait.get("wait_started_epoch", wait.get("timestamp_epoch"))
        if isinstance(started, int | float):
            holder = wait.get("holder")
            holder_fields = cast(dict[str, object], holder) if isinstance(holder, dict) else {}
            attribution = wait.get("wait_attribution")
            result.append(PhaseWait(str(wait.get("wait_reason") or wait.get("wait_kind") or "wait"),
                                    float(started), _wait_holder(holder_fields), str(wait.get("wait_kind", "")),
                                    str(holder_fields.get("resource_kind", "")), str(holder_fields.get("resource", "")),
                                    str(wait.get("pass_instance_id", "")), str(wait.get("owner_session", "")),
                                    cast(dict[str, object], attribution) if isinstance(attribution, dict) else {}))
    return result

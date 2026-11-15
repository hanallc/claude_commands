"""Read a live step record as a fixture row.

Fixture processes import this at each start, so it imports only the step record modules.
"""
from __future__ import annotations

from scripts.direct import step_record
from scripts.direct.step_selection import Pinned


def read_step_row(call_id: str) -> dict[str, object]:
    """Adapt a decoded record for existing fixture observations."""
    record = step_record.read(call_id)
    if not isinstance(record, step_record.StepRecord):
        raise AssertionError(f"The live record is invalid: {record}")
    location: dict[str, object]
    match record.runs_on:
        case step_record.AnyTarget(_, mac_targets):
            location = {"kind": "any_target", "mac_targets": list(mac_targets)}
        case step_record.NatedevOnly(reason):
            location = {"kind": "natedev_only", "reason": reason}
    return dict(kind=record.kind, state=record.state.value, selected_target=record.selected_target,
                updated=record.updated, runs_on=location, eligible_targets=list(record.eligible_targets),
                pinned_target=record.selection.target if isinstance(record.selection, Pinned) else "",
                package=step_record.package_name(record.package))

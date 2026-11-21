"""Resolve the List producer from its configured staff ID."""
from dataclasses import dataclass

from scripts.lib import conf
from scripts.production import staff


def list_producer(members: staff.StaffSnapshot, settings: conf.ConfigRead | None = None) -> staff.StaffRow | None:
    """Find the active List producer in the supplied staff snapshot."""
    identity = staff.list_producer_id(settings)
    row = members.by_id(identity) if identity is not None else None
    if (row is None or row.kind != "producer" or row.id in members.pending_retire
            or isinstance(row.retirement, staff.RetiredProducer) or isinstance(row.exited, staff.Exited)
            or isinstance(row.doc, staff.WrappedDoc) or isinstance(row.worktree, staff.RemovedWorktree)):
        return None
    return row


@dataclass(frozen=True)
class ListProducerHolder:
    """The native conversation on the active List producer row."""

    native: staff.StaffId


@dataclass(frozen=True)
class NoListProducerHolder:
    """No active staff row holds the List role."""


def holder() -> ListProducerHolder | NoListProducerHolder:
    """Use the live staff record."""
    try:
        row = list_producer(staff.snapshot())
    except staff.StaffUnreadable:
        return NoListProducerHolder()
    return ListProducerHolder(row.current_session) if row is not None else NoListProducerHolder()

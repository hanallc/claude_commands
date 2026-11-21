"""Keep member titles on their staff names and preserve ad hoc session titles.

The hook never stops the turn.
"""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import json
import os
import sys
from collections.abc import Mapping
from typing import TypedDict, cast

from scripts.herdr import herdr, worker_panes  # noqa: E402
from scripts.herdr import herdr_types  # noqa: E402
from scripts.message import sessions  # noqa: E402
from scripts.production import staff
from scripts.agents import pane_record
from scripts.herdr import pane_launch


class StopPayload(TypedDict, total=False):
    session_id: str


def current_name(session_id: str) -> str:
    """Read the session name. An empty name means a complete read found no record."""
    registry = sessions.read_registry()
    records = _title_records(session_id, registry)
    if not records and registry.unreadable:
        raise OSError("session registry unreadable")
    return records[0]["name"] if records else ""


def _title_records(session_id: str, registry: sessions.RegistryRead) -> tuple[sessions.SessionRecord, ...]:
    return tuple(item.record for item in registry.entries
                 if item.record["sessionId"] == session_id and not sessions.headless(item.record))


def _clear_handoff_pending(session_id: str, shared: str, pane: str, record: staff.StaffSnapshot,
                           records: tuple[sessions.SessionRecord, ...]) -> bool:
    """Match the current process to a retained member launch before reporting an incomplete clear."""
    try:
        entry = pane_record.read_strict(shared).get(pane)
    except (OSError, ValueError):
        return False
    if entry is None or pane_launch.reference_id(entry) == session_id or entry.get("ended"):
        return False
    pid, started = entry.get("pid"), entry.get("proc_start")
    if (not isinstance(pid, int) or isinstance(pid, bool) or not started
            or record.by_member_reference(pane_launch.reference_id(entry)) is None):
        return False
    return any(current["pid"] == pid and current.get("procStart") == started
               for current in records) and sessions.process_start(pid) == started


def sync(payload: StopPayload, environment: Mapping[str, str]) -> bool:
    """Use the current runtime row for member titles. Return whether a title changed."""
    pane = environment.get("HERDR_PANE_ID", "")
    session_id = payload.get("session_id", "")
    if not pane or not session_id or environment.get("TMUX_PANE"):
        return False
    shared = herdr.shared_session()
    if environment.get("HERDR_SESSION") != shared:
        return False
    registry = sessions.read_registry()
    records = _title_records(session_id, registry)
    name = records[0]["name"] if records else ""
    try:
        record = staff.snapshot()
    except staff.StaffAbsent:
        record = staff.StaffSnapshot(())
    except (OSError, ValueError) as error:
        print(f"herdr title: cannot read staff: {error}", file=sys.stderr)
        return False
    row = record.by_current_session(session_id)
    if row is not None:
        if row.kind == "unit" and name and name != row.name:
            print(f"herdr title: {name!r} differs from unit {row.name!r}; use /agents rename.", file=sys.stderr)
        client = herdr.Herdr(shared)
        title = (worker_panes.agent_name(record.rows_by_id[cast(staff.ReportsTo, row.parent).id], row.name)
                 if row.kind == "worker" else row.name)
        return client.retitle(pane, title, tab=row.kind != "worker")
    if not records and registry.unreadable:
        print(f"herdr title: session registry unreadable for {session_id}.", file=sys.stderr)
        return False
    if _clear_handoff_pending(session_id, shared, pane, record, records):
        print(f"herdr title: no staff row for current session {session_id}.", file=sys.stderr)
    if not name:
        return False
    client = herdr.Herdr(shared)
    panes = client.panes()
    tab = next((item.get("tab_id") for item in panes if item["pane_id"] == pane), None)
    if any(item["pane_id"] != pane and item.get("tab_id") == tab for item in panes):
        return False
    return client.retitle(pane, name)


def main() -> int:
    try:
        payload = cast(StopPayload, json.loads(sys.stdin.read() or "{}"))
        _ = sync(payload, os.environ)
    except (OSError, ValueError, herdr_types.HerdrFailure) as error:
        print(f"herdr title: {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

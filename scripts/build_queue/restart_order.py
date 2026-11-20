"""Keep the queue time of a test stopped by a hold."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import cast

from scripts.build_queue import admission_protocol as wire, admission_records as records, execution_identity, landing_test_gate
from scripts.build_hold.owning_session import OwningSession
from scripts.buildlog import store, treekey
from scripts.lib.boot_identity import boot_identity
from scripts.production import staff


@dataclass(frozen=True)
class TestRestartKey:
    staff_id: staff.StaffId
    tree: str
    command: tuple[str, ...]


@dataclass(frozen=True)
class TestOrder:
    key: TestRestartKey
    owner: wire.Owner
    queued_at: float
    submitted_at: float
    call: records.Call
    waits_for_landing: bool = False


@dataclass(frozen=True)
class TestIdentityUnavailable:
    """The request has no stable identity for a restart."""


def test_key(request: landing_test_gate.TestRequest) -> TestRestartKey | TestIdentityUnavailable:
    if request.profile.step not in landing_test_gate.TEST_STEPS:
        return TestIdentityUnavailable()
    session = request.identity.session_id
    if isinstance(request, wire.PlacementRequest):
        if not session and isinstance(request.constraints.session, OwningSession):
            session = request.constraints.session.session_id
        source = request.constraints.source_key
        if not isinstance(source, execution_identity.SourceKeyKnown):
            return TestIdentityUnavailable()
        tree = source.key
        command = request.constraints.command
    elif isinstance(request, wire.AdmitRequest):
        tree, command = "", request.identity.test_command or request.argv
    else:
        return TestIdentityUnavailable()
    if not session or not command:
        return TestIdentityUnavailable()
    try:
        member = staff.local_by_current_session(session.removeprefix("codex:").removeprefix("session:"))
    except (staff.StaffUnreadable, staff.StaffRefused):
        return TestIdentityUnavailable()
    if member is None:
        return TestIdentityUnavailable()
    if isinstance(request, wire.AdmitRequest):
        tree = treekey.tree_key(request.cwd) or ""
    return TestRestartKey(member.id, tree, command) if tree else TestIdentityUnavailable()


@dataclass(frozen=True)
class HoldStops:
    owners: frozenset[wire.Owner]
    calls: frozenset[records.CallRecord]

    def includes(self, order: TestOrder) -> bool:
        return order.owner in self.owners or order.call in self.calls


def held_stops(proc: Path, since: float) -> HoldStops:
    """Read confirmed hold stops on this boot since the test entered the queue."""
    owners: set[wire.Owner] = set()
    calls: set[records.CallRecord] = set()
    try:
        boot = boot_identity(proc)
    except (OSError, ValueError):
        return HoldStops(frozenset(), frozenset())
    for path in (store.root() / store.host_name()).glob("hold-builds-*.jsonl"):
        try:
            if path.stat().st_mtime < since:
                continue
            for line in path.read_text().splitlines():
                entry = cast(dict[str, object], json.loads(line))
                if (entry.get("boot_id") != boot or entry.get("action") != "stopped"
                        or entry.get("stop_status") == "requested"):
                    continue
                pid, start = entry.get("pid"), entry.get("proc_start")
                if isinstance(pid, int) and isinstance(start, str):
                    owners.add(wire.Owner(pid, start))
                call = entry.get("call_id")
                if isinstance(call, str) and call:
                    calls.add(records.CallRecord(call))
        except (OSError, ValueError, AttributeError):
            continue
    return HoldStops(frozenset(owners), frozenset(calls))

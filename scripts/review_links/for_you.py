"""Read user actions and rebuild requirements without changing either store."""
from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from http.client import HTTPConnection, HTTPException
from ipaddress import IPv4Address
from pathlib import Path
from typing import Literal, cast

from scripts.for_you.action import ActionStore
from scripts.direct import recorder_state
from scripts.lib import conf
from scripts.production import staff
from scripts.rebuild import need
from scripts.statusline import rebuild_row


@dataclass(frozen=True)
class RebuildRequest:
    """One rebuild request with its display name, waiting work, and switch evidence."""

    machine: str
    reason: str
    requested_by: str
    concerns: str
    created_at: str
    evidence: str
    waits: str


@dataclass(frozen=True)
class ForYouRow:
    """One action card or one machine card with all of its rebuild requests."""

    key: str
    kind: Literal["rebuild", "action"]
    title: str
    command: str
    machine: str
    cwd: str
    requested_by: str
    reason: str
    waits: str
    created_at: str
    status: Literal["open", "done", "withdrawn", "unknown"]
    completion: Literal["manual", "automatic"]
    evidence: str
    requests: tuple[RebuildRequest, ...] = ()


@dataclass(frozen=True)
class UnavailableSource:
    """A failed or stale source prevents a complete action view."""

    source: str
    reason: str


@dataclass(frozen=True)
class KnownOpenCount:
    """The complete number of open rebuild rows and agent actions."""

    value: int


@dataclass(frozen=True)
class UnavailableOpenCount:
    """The complete count is unavailable; known_open counts only confirmed open rows."""

    known_open: int


@dataclass(frozen=True)
class ForYouSnapshot:
    """The available rows and explicit failures from one read."""

    rows: tuple[ForYouRow, ...]
    unavailable: tuple[UnavailableSource, ...]

    @property
    def open_count(self) -> KnownOpenCount | UnavailableOpenCount:
        count = sum(row.status == "open" for row in self.rows)
        return UnavailableOpenCount(count) if self.unavailable else KnownOpenCount(count)


def _request(value: Mapping[str, object], names: Mapping[str, str], waiting_work: Callable[[str], str],
             *, completed: bool) -> RebuildRequest:
    fields: dict[str, str] = {}
    for key in ("machine", "reason", "requested_by", "concerns", "created_at", "id", "nixos_commit"):
        item = value[key]
        if not isinstance(item, str):
            raise ValueError(f"{key}: expected text.")
        fields[key] = item
    if fields["machine"] not in ("natedev", "mac"):
        raise ValueError("Unknown rebuild machine.")
    if datetime.fromisoformat(fields["created_at"]).tzinfo is None:
        raise ValueError("A rebuild time requires a time zone.")
    evidence = value.get("cleared_by", "")
    if not isinstance(evidence, str) or (completed and not evidence.strip()):
        raise ValueError("A completed rebuild need requires switch evidence.")
    requester = fields["requested_by"]
    concerns = fields["concerns"]
    return RebuildRequest(fields["machine"], fields["reason"], names.get(requester, requester),
                          names.get(concerns, concerns), fields["created_at"], evidence, waiting_work(concerns))


def _completed_requests(names: Mapping[str, str], waiting_work: Callable[[str], str]) -> tuple[RebuildRequest, ...]:
    folder = need.needs_folder() / "done"
    try:
        paths = sorted(path for path in folder.iterdir() if path.suffix == ".json")
    except FileNotFoundError:
        return ()
    records: list[RebuildRequest] = []
    for path in paths:
        value = cast(object, json.loads(path.read_text(encoding="utf-8")))
        if not isinstance(value, dict):
            raise ValueError(f"{path.name}: expected a rebuild record.")
        records.append(_request(cast(dict[str, object], value), names, waiting_work, completed=True))
    return tuple(records)


def read(config: Path = rebuild_row.DEFAULT_CONFIG, *,
         rebuild: rebuild_row.RebuildAnswer | None = None) -> ForYouSnapshot:
    """Project both stores and the comparison cache without running a rebuild or a refresh."""
    unavailable: list[UnavailableSource] = []
    names: dict[str, str] = {}
    members_by_reference: dict[str, staff.StaffRow] = {}
    try:
        members = staff.snapshot()
        for member in members.rows:
            names[str(member.id)] = member.name
            members_by_reference[str(member.id)] = member
            if member.current_session:
                names[member.current_session] = member.name
                members_by_reference[member.current_session] = member
    except (OSError, ValueError) as error:
        unavailable.append(UnavailableSource("staff", str(error)))

    work_by_reference: dict[str, str] = {}

    def waiting_work(reference: str) -> str:
        if not reference:
            return "Not recorded"
        if reference in work_by_reference:
            return work_by_reference[reference]
        name, work = names.get(reference, reference), "Not recorded"
        member = members_by_reference.get(reference)
        if member is not None:
            holder = member
            if member.kind == "worker" and isinstance(member.parent, staff.ReportsTo):
                holder = members_by_reference.get(str(member.parent.id), member)
            if isinstance(holder.run, staff.OpenRun):
                try:
                    work = ", ".join(recorder_state.read_open_phase_ids(holder.run.path)) or "Not recorded"
                except (OSError, ValueError) as error:
                    unavailable.append(UnavailableSource(f"staff work {reference}", str(error)))
        work_by_reference[reference] = f"{name}: {work}"
        return work_by_reference[reference]

    rows: list[ForYouRow] = []
    try:
        for record in ActionStore().list():
            rows.append(ForYouRow(record.key, "action", record.title, record.command, record.machine, record.cwd,
                                  names.get(record.requested_by, record.requested_by), record.reason, record.waits,
                                  record.created_at, record.status, record.completion,
                                  record.evidence or record.withdraw_reason))
    except (OSError, ValueError) as error:
        unavailable.append(UnavailableSource("actions", str(error)))

    answer = rebuild if rebuild is not None else rebuild_row.read(config)
    pending: tuple[RebuildRequest, ...] = ()
    needs = answer.needs
    needs_available = isinstance(needs, tuple)
    if isinstance(needs, rebuild_row.RebuildNeedsNotRead):
        unavailable.append(UnavailableSource("rebuild needs", "The open rebuild records have not been read."))
    elif isinstance(needs, rebuild_row.RebuildNeedsUnavailable):
        unavailable.append(UnavailableSource("rebuild needs", needs.reason))
    else:
        try:
            pending = tuple(_request(record, names, waiting_work, completed=False)
                            for summary in needs for record in summary.records)
        except (OSError, ValueError, KeyError, TypeError) as error:
            needs_available = False
            unavailable.append(UnavailableSource("rebuild needs", str(error)))

    history: tuple[RebuildRequest, ...] = ()
    try:
        history = _completed_requests(names, waiting_work)
    except (OSError, ValueError, KeyError, TypeError) as error:
        unavailable.append(UnavailableSource("completed rebuild needs", str(error)))

    compared: tuple[str, ...] = ()
    current_comparisons: set[str] = set()
    working_directories: dict[str, str] = {}
    try:
        values = rebuild_row.config_values(config)
        for machine in ("natedev", "mac"):
            directory = values[f"rebuild.cwd.{machine}"]
            if not (Path(directory).is_absolute() or directory.startswith("~/")):
                raise ValueError(f"rebuild.cwd.{machine}: provide an absolute path or a home path.")
            working_directories[machine] = directory
        _ = rebuild_row.RebuildSettings.read(config)
        compared = answer.compared
        if answer.refresh_due:
            unavailable.append(UnavailableSource("rebuild comparison", "The comparison is stale or unavailable."))
        else:
            for machine in ("natedev", "mac"):
                if not answer.evaluation_current(machine):
                    unavailable.append(UnavailableSource("rebuild comparison", f"{machine}: the expected system is unavailable."))
                elif not answer.probes.succeeded(machine):
                    unavailable.append(UnavailableSource("rebuild comparison", f"{machine}: the running system is unavailable."))
                else:
                    current_comparisons.add(machine)
    except (OSError, ValueError, KeyError, TypeError) as error:
        unavailable.append(UnavailableSource("rebuild comparison", str(error)))

    machines = {request.machine for request in (*pending, *history)} | set(compared)
    for machine in sorted(machines):
        active = tuple(request for request in pending if request.machine == machine)
        completed = tuple(request for request in history if request.machine == machine)
        requests = tuple(sorted((*active, *completed), key=lambda request: datetime.fromisoformat(request.created_at)))
        status: Literal["open", "done", "unknown"] = (
            "open" if active or machine in compared else "done"
            if machine in current_comparisons and needs_available else "unknown")
        current = active if active else completed
        reason = "\n".join(f"{request.reason} Requester: {request.requested_by or 'Not recorded'}. "
                           + f"Concerns: {request.concerns or 'Not recorded'}." for request in current)
        if machine in compared:
            reason = (reason + "\n" if reason else "") + "The running system differs from the configured system."
        rows.append(ForYouRow(
            f"rebuild/{machine}", "rebuild", f"Rebuild {machine}", "rebuild", machine, working_directories.get(machine, ""),
            ", ".join(dict.fromkeys(request.requested_by for request in current if request.requested_by)),
            reason, "; ".join(dict.fromkeys(request.waits for request in active)) or "Not recorded",
            requests[0].created_at if requests else "", status, "automatic",
            "\n".join(request.evidence for request in completed), requests))
    return ForYouSnapshot(tuple(rows), tuple(unavailable))


def global_action_count(rebuild: rebuild_row.RebuildAnswer | None = None) -> str:
    """Return a complete action count or an explicit unavailable result."""
    try:
        count = read(rebuild_row.DEFAULT_CONFIG, rebuild=rebuild).open_count
    except (OSError, ValueError):
        return "unavailable"
    return str(count.value) if isinstance(count, KnownOpenCount) else "unavailable"


def console_action_count() -> str:
    """Read the global count from the console host."""
    try:
        host = os.environ.get("REVIEW_LINKS_HOST")
        if not host:
            peer = conf.read_or_repository("console").text("for_you.host")
            address = subprocess.check_output(
                ["tailscale", "ip", "-4", peer], text=True, stderr=subprocess.DEVNULL, timeout=2)
            host = str(IPv4Address(address.strip()))
        port = conf.read_or_repository("review_links").whole("server_port")
        connection = HTTPConnection(host, port, timeout=2)
        try:
            connection.request("GET", "/console/for-you.json?view=count")
            response = connection.getresponse()
            if response.status != 200:
                return "unavailable"
            payload = cast(object, json.loads(response.read()))
        finally:
            connection.close()
    except (HTTPException, OSError, ValueError, subprocess.SubprocessError):
        return "unavailable"
    if isinstance(payload, dict):
        fields = cast(dict[str, object], payload)
        count = fields.get("count")
        if fields.get("available") is True and type(count) is int and count >= 0:
            return str(count)
    return "unavailable"


def action_count(source: str, rebuild: rebuild_row.RebuildAnswer | None = None) -> str:
    """Read the action count from its selected source."""
    return console_action_count() if source == "--for-you-console" else global_action_count(rebuild)

"""Read user actions and rebuild requirements without changing either store."""
from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from http.client import HTTPConnection, HTTPException
from ipaddress import IPv4Address
from pathlib import Path
from typing import Literal, cast

from scripts.for_you.action import ActionStore, UserAction
from scripts.direct import recorder_state
from scripts.lib import conf
from scripts.production import staff
from scripts.rebuild import need
from scripts.statusline import rebuild_row


@dataclass(frozen=True)
class RebuildRequest:
    """One open rebuild request with its requester and reason."""

    machine: str
    reason: str
    requested_by: str
    concerns: str
    created_at: str
    waits: str
    requester_reference: str


@dataclass(frozen=True)
class ForYouRow:
    """One action card or one machine card with its open rebuild requests."""

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
    updated_at: str = ""
    requester_reference: str = ""
    running_revision: str = ""


@dataclass(frozen=True)
class UnavailableSource:
    """A failed or stale source prevents a complete action view."""

    source: str
    reason: str
    partial: bool = False


@dataclass(frozen=True)
class KnownOpenCount:
    """The complete number of open machine cards and agent actions."""

    value: int


@dataclass(frozen=True)
class UnavailableOpenCount:
    """The complete count is unavailable; known_open counts only confirmed open cards."""

    known_open: int


class OtherActionCountUnavailable(StrEnum):
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class StatusLineActions:
    """The waiting rebuilds and the count of other open actions."""

    rebuild_machines: tuple[str, ...] = ()
    other_count: int | OtherActionCountUnavailable = OtherActionCountUnavailable.UNAVAILABLE
    partial: bool = False


@dataclass(frozen=True)
class ForYouSnapshot:
    """The available rows and explicit failures from one read."""

    rows: tuple[ForYouRow, ...]
    unavailable: tuple[UnavailableSource, ...]
    rebuild_machines: tuple[str, ...] = ()
    comparison_refresh_due: bool = False
    comparison_deadline: int = 0

    @property
    def open_count(self) -> KnownOpenCount | UnavailableOpenCount:
        count = sum(row.status == "open" for row in self.rows)
        return UnavailableOpenCount(count) if self.unavailable else KnownOpenCount(count)

    def status_line_actions(self) -> StatusLineActions:
        if any(not source.partial for source in self.unavailable):
            return StatusLineActions(self.rebuild_machines)
        count = sum(row.kind == "action" and row.status == "open" for row in self.rows)
        return StatusLineActions(self.rebuild_machines, count, partial=bool(self.unavailable))


def _request(value: Mapping[str, object], names: Mapping[str, str], waiting_work: Callable[[str], str]) -> RebuildRequest:
    fields: dict[str, str] = {}
    for key in ("machine", "reason", "requested_by", "concerns", "created_at", "id", "nixos_commit"):
        item = value.get(key, "") if key == "reason" else value[key]
        if not isinstance(item, str):
            raise ValueError(f"{key}: expected text.")
        fields[key] = item
    if fields["machine"] not in ("natedev", "mac"):
        raise ValueError("Unknown rebuild machine.")
    created_at = need.request_time(fields["created_at"]).isoformat()
    requester = fields["requested_by"]
    concerns = fields["concerns"]
    return RebuildRequest(fields["machine"], fields["reason"].strip(), names.get(requester, requester),
                          names.get(concerns, concerns), created_at, waiting_work(concerns), requester)


def _watch_completed_requests(watch: need.SourceWatch) -> None:
    """Track archive changes for the cache without reading completed requests."""
    folder = need.needs_folder() / "done"
    watch.watch(folder)
    try:
        for path in folder.iterdir():
            if path.suffix == ".json":
                watch.watch(path)
    except FileNotFoundError:
        if not watch.verified_absence(folder):
            watch.mark_incomplete()
    except OSError:
        watch.mark_incomplete()


def action_row(record: UserAction, requester: str) -> ForYouRow:
    """Project one stored action for Review and the notifier."""
    return ForYouRow(record.key, "action", record.title, record.command, record.machine, record.cwd,
                     requester, record.reason, record.waits, record.created_at, record.status, record.completion,
                     record.evidence or record.withdraw_reason, updated_at=record.updated_at,
                     requester_reference=record.requested_by)


def rebuild_config() -> Path:
    return conf.path("statusline") if "CONF_DIR" in os.environ else rebuild_row.DEFAULT_CONFIG


def action_source_paths() -> tuple[Path, Path, Path]:
    needs = need.needs_folder()
    return ActionStore().folder, needs, needs / "done"


def source_paths(config: Path | None = None) -> tuple[Path, ...]:
    """Watch the native action sources and the rebuild comparison."""
    config = rebuild_config() if config is None else config
    paths = (*action_source_paths(), config)
    try:
        cache = rebuild_row.cache_path(config)
        return (*paths, cache, rebuild_row.changes_path(cache))
    except (OSError, ValueError, KeyError):
        return paths


def _open_phase_ids(folder: Path, watch: need.SourceWatch | None) -> list[str]:
    if watch is None:
        return recorder_state.read_open_phase_ids(folder)
    path = folder / recorder_state.STATE_FILENAME
    watch.watch(path)
    try:
        return recorder_state.open_phase_ids(recorder_state.read_state(path))
    except FileNotFoundError:
        if not watch.verified_absence(path):
            raise
        return []


def read(config: Path | None = None, *,
         rebuild: rebuild_row.RebuildAnswer | None = None, members: staff.StaffSnapshot | None = None,
         include_running_revision: bool = True, watch: need.SourceWatch | None = None) -> ForYouSnapshot:
    """Project both stores and the comparison cache without running a rebuild or a refresh."""
    config = rebuild_config() if config is None else config
    unavailable: list[UnavailableSource] = []
    names: dict[str, str] = {}
    members_by_reference: dict[str, staff.StaffRow] = {}
    try:
        if watch is not None and members is None:
            watch.watch(staff.record_path())
        members = staff.snapshot() if members is None else members
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
                    work = ", ".join(_open_phase_ids(holder.run.path, watch)) or "Not recorded"
                except (OSError, ValueError) as error:
                    unavailable.append(UnavailableSource(f"staff work {reference}", str(error)))
        work_by_reference[reference] = f"{name}: {work}"
        return work_by_reference[reference]

    rows: list[ForYouRow] = []
    try:
        store = ActionStore()
        for record in store.list() if watch is None else store.list(watch=watch.watch):
            rows.append(action_row(record, names.get(record.requested_by, record.requested_by)))
    except (OSError, ValueError) as error:
        unavailable.append(UnavailableSource("actions", str(error)))

    answer = rebuild if rebuild is not None else rebuild_row.read(config, watch=watch)
    if watch is not None:
        _watch_completed_requests(watch)
    pending: tuple[RebuildRequest, ...] = ()
    needs = answer.needs
    if isinstance(needs, rebuild_row.RebuildNeedsNotRead):
        unavailable.append(UnavailableSource("rebuild needs", "The open rebuild records have not been read."))
    elif isinstance(needs, rebuild_row.RebuildNeedsUnavailable):
        unavailable.append(UnavailableSource("rebuild needs", needs.reason, partial=needs.summaries is not None))
    try:
        pending = tuple(_request(record, names, waiting_work)
                        for summary in needs for record in summary.records)
    except (OSError, ValueError, KeyError, TypeError) as error:
        unavailable.append(UnavailableSource("rebuild needs", str(error)))

    compared: tuple[str, ...] = ()
    comparison_current = False
    probes = rebuild_row.RunningSystemProbes()
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
        probes = answer.probes
        comparison_current = (not answer.refresh_due and isinstance(answer.expected, rebuild_row.ExpectedSystems)
                              and isinstance(answer.failure, rebuild_row.NoEvaluationFailure))
        if not comparison_current:
            unavailable.append(UnavailableSource("rebuild comparison", "The comparison is stale or unavailable."))
        else:
            for machine in ("natedev", "mac"):
                if not probes.succeeded(machine):
                    unavailable.append(UnavailableSource("rebuild comparison", f"{machine}: the running system is unavailable."))
    except (OSError, ValueError, KeyError, TypeError) as error:
        unavailable.append(UnavailableSource("rebuild comparison", str(error)))

    machines = {request.machine for request in pending} | set(compared)
    changes = (rebuild_row.read_compared_changes(config, answer)
               if include_running_revision and comparison_current and machines else {})
    for machine in sorted(machines):
        active = tuple(sorted((request for request in pending if request.machine == machine),
                              key=lambda request: need.request_time(request.created_at)))
        requesters = {request.requester_reference for request in active if request.requester_reference}
        change = changes.get(machine)
        rows.append(ForYouRow(
            f"rebuild/{machine}", "rebuild", f"Rebuild {machine}", "rebuild", machine, working_directories.get(machine, ""),
            ", ".join(dict.fromkeys(request.requested_by for request in active if request.requested_by)),
            "", "; ".join(dict.fromkeys(request.waits for request in active)) or "Not recorded",
            active[0].created_at if active else "", "open", "automatic", "", active,
            requester_reference=next(iter(requesters)) if len(requesters) == 1 else "",
            running_revision=change.active_revision if change is not None else ""))
    return ForYouSnapshot(tuple(rows), tuple(unavailable), answer.machines, answer.refresh_due,
                          answer.comparison_deadline)


def read_for_status_line(config: Path = rebuild_row.DEFAULT_CONFIG, *,
                         rebuild: rebuild_row.RebuildAnswer | None = None,
                         watch: need.SourceWatch | None = None) -> ForYouSnapshot:
    """Keep the waiting rebuild names if a read exceeds the recursion limit."""
    try:
        return read(config, rebuild=rebuild, include_running_revision=False, watch=watch)
    except RecursionError as error:
        answer = rebuild if rebuild is not None else rebuild_row.read(config, watch=watch)
        return ForYouSnapshot((), (UnavailableSource("For you", str(error)),), answer.machines, answer.refresh_due,
                              answer.comparison_deadline)


def global_action_count(rebuild: rebuild_row.RebuildAnswer | None = None) -> str:
    """Return a complete action count or an explicit unavailable result."""
    try:
        count = read(rebuild_row.DEFAULT_CONFIG, rebuild=rebuild, include_running_revision=False).open_count
    except (OSError, ValueError):
        return "unavailable"
    return str(count.value) if isinstance(count, KnownOpenCount) else "unavailable"


@dataclass(frozen=True)
class ConsoleAddress:
    host: str
    port: int


def console_address() -> ConsoleAddress:
    """Use the host override or the Tailscale address of the console host, with the server port."""
    host = os.environ.get("REVIEW_LINKS_HOST")
    if not host:
        peer = conf.read_or_repository("console").text("for_you.host")
        address = subprocess.check_output(
            ["tailscale", "ip", "-4", peer], text=True, stderr=subprocess.DEVNULL, timeout=2)
        host = str(IPv4Address(address.strip()))
    return ConsoleAddress(host, conf.read_or_repository("review_links").whole("server_port"))


def _console_count_payload() -> dict[str, object]:
    """Read the count view from the console host."""
    try:
        address = console_address()
        connection = HTTPConnection(address.host, address.port, timeout=2)
        try:
            connection.request("GET", "/console/for-you.json?view=count")
            response = connection.getresponse()
            if response.status != 200:
                return {}
            payload = cast(object, json.loads(response.read()))
        finally:
            connection.close()
    except (HTTPException, OSError, ValueError, subprocess.SubprocessError):
        return {}
    return cast(dict[str, object], payload) if isinstance(payload, dict) else {}


def console_action_count() -> str:
    """Read the global count from the console host."""
    fields = _console_count_payload()
    count = fields.get("count")
    return str(count) if fields.get("available") is True and type(count) is int and count >= 0 else "unavailable"


def console_status_line_actions() -> StatusLineActions:
    """Read the rebuild machines and other action count from the console host."""
    fields = _console_count_payload()
    machines = fields.get("rebuild_machines")
    supplied = cast(list[object], machines) if isinstance(machines, list) else []
    rebuilds = tuple(machine for machine in rebuild_row.MACHINES if machine in supplied)
    partial = fields.get("partial") is True
    if fields.get("available") is True or partial:
        if "other_count" not in fields and "rebuild_machines" not in fields:
            count = fields.get("count")
        else:
            count = fields.get("other_count")
        if type(count) is int and count >= 0:
            return StatusLineActions(rebuilds, count, partial=partial)
    return StatusLineActions(rebuilds)


def action_count(source: str, rebuild: rebuild_row.RebuildAnswer | None = None) -> str:
    """Read the action count from its selected source."""
    return console_action_count() if source == "--for-you-console" else global_action_count(rebuild)

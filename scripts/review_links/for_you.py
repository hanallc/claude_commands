"""Read user actions and rebuild requirements without changing either store."""
from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from http.client import HTTPConnection, HTTPException
from ipaddress import IPv4Address
from pathlib import Path
from threading import Lock
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
    """A source failure and whether it leaves its count known."""

    source: str
    reason: str
    partial: bool = False
    count_retained: bool = False


@dataclass(frozen=True)
class KnownOpenCount:
    """The complete number of open machine cards and agent actions."""

    value: int


class ForYouCountPart(StrEnum):
    ACTIONS = "actions"
    REBUILD_REQUESTS = "rebuild requests"
    REBUILD_COMPARISON = "rebuild comparison"


@dataclass(frozen=True)
class StaleOpenCount:
    """The count with one or more parts from an earlier complete read."""

    value: int
    kept_parts: tuple[ForYouCountPart, ...]


@dataclass(frozen=True)
class PartialOpenCount:
    """The count that includes readable records from an incomplete source."""

    value: int
    affected_parts: tuple[ForYouCountPart, ...]


@dataclass(frozen=True)
class UnavailableOpenCount:
    """At least one count part has no usable value."""


type ForYouOpenCount = KnownOpenCount | StaleOpenCount | PartialOpenCount | UnavailableOpenCount


@dataclass(frozen=True)
class CountFromRows:
    """Count the rows when the caller supplies an already assembled snapshot."""


@dataclass(frozen=True)
class CurrentCountPart[T]:
    """A count part from a complete read of its source."""

    value: T


@dataclass(frozen=True)
class KeptCountPart[T]:
    """A count part that uses an earlier observation."""

    value: T


@dataclass(frozen=True)
class ReadableCountPart[T]:
    """A count part from the readable records of a partial read."""

    value: T


@dataclass(frozen=True)
class MissingCountPart:
    """The current read failed and there is no earlier value."""


type CountPartRead[T] = CurrentCountPart[T] | KeptCountPart[T] | ReadableCountPart[T] | MissingCountPart


def _open_count(actions: CountPartRead[int], requests: CountPartRead[frozenset[str]],
                compared: CountPartRead[set[str]]) -> ForYouOpenCount:
    if (isinstance(actions, MissingCountPart) or isinstance(requests, MissingCountPart)
            or isinstance(compared, MissingCountPart)):
        return UnavailableOpenCount()
    value = actions.value + len(requests.value | compared.value)
    parts = ((ForYouCountPart.ACTIONS, actions), (ForYouCountPart.REBUILD_REQUESTS, requests),
             (ForYouCountPart.REBUILD_COMPARISON, compared))
    affected = tuple(part for part, result in parts if not isinstance(result, CurrentCountPart))
    if any(isinstance(result, ReadableCountPart) for _, result in parts):
        return PartialOpenCount(value, affected)
    return StaleOpenCount(value, affected) if affected else KnownOpenCount(value)


class OtherActionCountUnavailable(StrEnum):
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class StatusLineActions:
    """The waiting rebuilds and the count of other open actions."""

    rebuild_machines: tuple[str, ...] = ()
    other_count: int | OtherActionCountUnavailable = OtherActionCountUnavailable.UNAVAILABLE
    partial: bool = False
    rebuild_reasons: Mapping[str, str] = field(default_factory=dict[str, str])

    def rebuild_text(self) -> str:
        return rebuild_row.rebuild_names(self.rebuild_machines, self.rebuild_reasons)


@dataclass(frozen=True)
class ForYouSnapshot:
    """The rows, staff names, and source failures from one read."""

    rows: tuple[ForYouRow, ...]
    unavailable: tuple[UnavailableSource, ...]
    rebuild_machines: tuple[str, ...] = ()
    comparison_refresh_due: bool = False
    comparison_deadline: int = 0
    rebuild_reasons: Mapping[str, str] = field(default_factory=dict[str, str])
    count: ForYouOpenCount | CountFromRows = CountFromRows()
    members: staff.StaffSnapshot = staff.StaffSnapshot(())

    @property
    def open_count(self) -> ForYouOpenCount:
        if not isinstance(self.count, CountFromRows):
            return self.count
        if self.unavailable:
            return UnavailableOpenCount()
        return KnownOpenCount(sum(row.status == "open" for row in self.rows))

    def status_line_actions(self) -> StatusLineActions:
        if any(not source.partial for source in self.unavailable):
            return StatusLineActions(self.rebuild_machines, rebuild_reasons=self.rebuild_reasons)
        count = sum(row.kind == "action" and row.status == "open" for row in self.rows)
        return StatusLineActions(self.rebuild_machines, count, partial=bool(self.unavailable),
                                 rebuild_reasons=self.rebuild_reasons)


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


def status_rebuild_reasons(answer: rebuild_row.RebuildAnswer,
                           changes: Mapping[str, rebuild_row.ComparedChanges]) -> dict[str, str]:
    """Read short reasons from the needs and the matched comparison."""
    reasons: dict[str, str] = {}
    for summary in answer.needs:
        for record in reversed(summary.records):
            if words := rebuild_row.short_reason(record["reason"]):
                reasons[record["machine"]] = words
                break
    for machine in answer.compared:
        if machine not in reasons:
            reasons[machine] = rebuild_row.comparison_reason(
                changes.get(machine), configured_subject=answer.configured_subject, compact=True)
    return reasons


@dataclass(frozen=True)
class ConfirmedSystemComparison:
    """The running and expected paths from a successful comparison."""

    running_path: str
    expected_path: str


_actions_lock = Lock()
_confirmed_open_actions: dict[Path, int] = {}
_comparison_lock = Lock()
_confirmed_rebuilds: dict[Path, dict[str, ConfirmedSystemComparison]] = {}
_confirmed_requests: dict[Path, rebuild_row.OpenRebuildNeeds] = {}


def _read_actions(watch: need.SourceWatch | None, unavailable: list[UnavailableSource]
                  ) -> tuple[tuple[UserAction, ...], CountPartRead[int]]:
    try:
        store = ActionStore()
    except (OSError, ValueError) as error:
        unavailable.append(UnavailableSource("actions", str(error)))
        return (), MissingCountPart()
    with _actions_lock:
        folder = store.folder.absolute()
        try:
            records = store.list() if watch is None else store.list(watch=watch.watch)
        except (OSError, ValueError, RecursionError) as error:
            unavailable.append(UnavailableSource("actions", str(error)))
            if folder in _confirmed_open_actions:
                return (), KeptCountPart(_confirmed_open_actions[folder])
            return (), MissingCountPart()
        count = sum(record.status == "open" for record in records)
        _confirmed_open_actions[folder] = count
        return records, CurrentCountPart(count)


def _read_rebuild_requests(needs: rebuild_row.RebuildNeedsRead, names: Mapping[str, str],
                           waiting_work: Callable[[str], str], unavailable: list[UnavailableSource]
                           ) -> tuple[tuple[RebuildRequest, ...], CountPartRead[frozenset[str]], rebuild_row.OpenRebuildNeeds]:
    folder = need.needs_folder().absolute()
    if isinstance(needs, tuple):
        summaries = needs
    elif isinstance(needs, rebuild_row.PartialRebuildNeeds):
        unavailable.append(UnavailableSource("rebuild needs", needs.reason, partial=True))
        summaries = needs.summaries
    else:
        reason = (needs.reason if isinstance(needs, rebuild_row.RebuildNeedsUnavailable)
                  else "The open rebuild records have not been read.")
        unavailable.append(UnavailableSource("rebuild needs", reason))
        if folder not in _confirmed_requests:
            return (), MissingCountPart(), ()
        summaries = _confirmed_requests[folder]

    pending: list[RebuildRequest] = []
    invalid = False
    for summary in summaries:
        for record in summary.records:
            try:
                pending.append(_request(record, names, waiting_work))
            except (OSError, ValueError, KeyError, TypeError) as error:
                unavailable.append(UnavailableSource("rebuild needs", str(error), partial=True))
                invalid = True
    machines = frozenset(request.machine for request in pending)
    if invalid or isinstance(needs, rebuild_row.PartialRebuildNeeds):
        return tuple(pending), ReadableCountPart(machines), summaries
    if isinstance(needs, tuple):
        _confirmed_requests[folder] = needs
        return tuple(pending), CurrentCountPart(machines), summaries
    return tuple(pending), KeptCountPart(machines), summaries


def _compared_machines(config: Path, answer: rebuild_row.RebuildAnswer,
                       confirmed: dict[str, ConfirmedSystemComparison],
                       watch: need.SourceWatch | None) -> tuple[set[str], tuple[UnavailableSource, ...]]:
    """Use the last confirmed result for each machine whose probe failed."""
    expected = answer.expected
    usable = (isinstance(expected, rebuild_row.ExpectedSystems)
              and isinstance(answer.failure, rebuild_row.NoEvaluationFailure))
    failures: list[UnavailableSource] = []
    if answer.refresh_due or not usable:
        failures.append(UnavailableSource("rebuild comparison", "The comparison is stale or unavailable.",
                                          count_retained=True))
    compared = set(answer.compared)
    expected_paths = {machine: evaluated.path for machine, evaluated in answer.evaluated_system_paths.items()}
    if isinstance(expected, rebuild_row.ExpectedSystems):
        expected_paths.update((machine, expected.path_for(machine)) for machine in rebuild_row.MACHINES)
    failed: list[str] = []
    for machine in rebuild_row.MACHINES:
        expected_path = expected_paths.get(machine, "")
        running_path = answer.running_paths.get(machine, "")
        saved = answer.confirmed_systems.get(machine)
        if not running_path and saved is not None:
            running_path = saved.path
        if not running_path and usable and answer.probes.succeeded(machine) and machine not in compared:
            # Older caches report an equal result without the running path.
            running_path = expected_path
        if running_path and expected_path:
            confirmed[machine] = ConfirmedSystemComparison(running_path, expected_path)
        if not answer.probes.succeeded(machine) or not expected_path:
            failed.append(machine)

    # The changes cache can supply a result before the first confirmed probe.
    recorded = (rebuild_row.read_recorded_changes(config, watch=watch)
                if any(machine in expected_paths and machine not in confirmed and machine not in compared for machine in failed)
                else {})
    for machine in rebuild_row.MACHINES:
        observation = confirmed.get(machine)
        expected_path = expected_paths.get(machine, "")
        previous = recorded.get(machine)
        if expected_path:
            if machine in failed and observation is not None:
                observation = ConfirmedSystemComparison(observation.running_path, expected_path)
                confirmed[machine] = observation
            elif observation is None and isinstance(previous, rebuild_row.ComparedChanges):
                observation = ConfirmedSystemComparison(previous.running_path, expected_path)
        if observation is not None and (machine in failed or (machine in answer.running_paths and expected_path)):
            if observation.running_path == observation.expected_path:
                compared.discard(machine)
            else:
                compared.add(machine)
        if machine in failed:
            reason = ("the running system is unavailable" if not answer.probes.succeeded(machine)
                      else "the expected system is unavailable")
            failures.append(UnavailableSource("rebuild comparison", f"{machine}: {reason}.",
                                              count_retained=observation is not None or machine in compared))
    return compared, tuple(failures)


def read(config: Path | None = None, *,
         rebuild: rebuild_row.RebuildAnswer | None = None, members: staff.StaffSnapshot | None = None,
         include_running_revision: bool = True, include_status_reasons: bool = False,
         watch: need.SourceWatch | None = None) -> ForYouSnapshot:
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
    except (OSError, ValueError, RecursionError) as error:
        members = staff.StaffSnapshot(())
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
                except (OSError, ValueError, RecursionError) as error:
                    unavailable.append(UnavailableSource(f"staff work {reference}", str(error)))
        work_by_reference[reference] = f"{name}: {work}"
        return work_by_reference[reference]

    records, actions_count = _read_actions(watch, unavailable)
    rows = [action_row(record, names.get(record.requested_by, record.requested_by)) for record in records]

    with _comparison_lock:
        answer = rebuild if rebuild is not None else rebuild_row.read(config, watch=watch)
        path = config.absolute()
        confirmed = _confirmed_rebuilds.get(path, {}).copy()
        compared, failures = _compared_machines(config, answer, confirmed, watch)
        if rebuild is None and confirmed:
            _confirmed_rebuilds[path] = confirmed
        pending, requests_count, needs = _read_rebuild_requests(answer.needs, names, waiting_work, unavailable)
    if watch is not None:
        _watch_completed_requests(watch)

    unavailable.extend(failures)
    compared_count: CountPartRead[set[str]]
    if not failures:
        compared_count = CurrentCountPart(compared)
    elif all(failure.count_retained for failure in failures):
        compared_count = KeptCountPart(compared)
    else:
        compared_count = MissingCountPart()
    comparison_current = (not answer.refresh_due and isinstance(answer.expected, rebuild_row.ExpectedSystems)
                          and isinstance(answer.failure, rebuild_row.NoEvaluationFailure))
    working_directories: dict[str, str] = {}
    try:
        values = rebuild_row.config_values(config)
        for machine in ("natedev", "mac"):
            directory = values[f"rebuild.cwd.{machine}"]
            if not (Path(directory).is_absolute() or directory.startswith("~/")):
                raise ValueError(f"rebuild.cwd.{machine}: provide an absolute path or a home path.")
            working_directories[machine] = directory
        _ = rebuild_row.RebuildSettings.read(config)
    except (OSError, ValueError, KeyError, TypeError) as error:
        unavailable.append(UnavailableSource("rebuild comparison", str(error)))

    machines = {request.machine for request in pending} | compared
    records_by_machine = {summary.newest["machine"]: summary.records for summary in needs}
    changes = (rebuild_row.read_compared_changes(config, answer, watch=watch)
               if (include_running_revision and machines) or (include_status_reasons and compared) else {})
    visible_machines = tuple(machine for machine in rebuild_row.MACHINES if machine in machines)
    compared_machines = tuple(machine for machine in rebuild_row.MACHINES if machine in compared)
    visible_answer = replace(answer, compared=compared_machines, needs=needs)
    rebuild_reasons = status_rebuild_reasons(visible_answer, changes) if include_status_reasons else {}
    for machine in sorted(machines):
        active = tuple(sorted((request for request in pending if request.machine == machine),
                              key=lambda request: need.request_time(request.created_at)))
        requesters = {request.requester_reference for request in active if request.requester_reference}
        change = changes.get(machine)
        reason = (rebuild_row.changes_for_display(visible_answer, machine, change, records_by_machine.get(machine, ())).text()
                  if include_running_revision else "")
        running_revision = change.active_revision if include_running_revision and comparison_current and change else ""
        rows.append(ForYouRow(
            f"rebuild/{machine}", "rebuild", f"Rebuild {machine}", "rebuild", machine, working_directories.get(machine, ""),
            ", ".join(dict.fromkeys(request.requested_by for request in active if request.requested_by)),
            reason, "; ".join(dict.fromkeys(request.waits for request in active)) or "Not recorded",
            active[0].created_at if active else "", "open", "automatic", "", active,
            requester_reference=next(iter(requesters)) if len(requesters) == 1 else "",
            running_revision=running_revision))
    return ForYouSnapshot(tuple(rows), tuple(unavailable), visible_machines, answer.refresh_due,
                          answer.comparison_deadline, rebuild_reasons,
                          _open_count(actions_count, requests_count, compared_count), members=members)


def read_for_status_line(config: Path | None = None, *,
                         rebuild: rebuild_row.RebuildAnswer | None = None, include_reasons: bool = False,
                         watch: need.SourceWatch | None = None) -> ForYouSnapshot:
    """Keep rebuild reasons when the action count is unavailable."""
    config = rebuild_config() if config is None else config
    try:
        return read(config, rebuild=rebuild, include_running_revision=False, include_status_reasons=include_reasons,
                    watch=watch)
    except RecursionError as error:
        answer = rebuild if rebuild is not None else rebuild_row.read(config, watch=watch)
        changes = rebuild_row.read_compared_changes(config, answer, watch=watch) if include_reasons and answer.compared else {}
        reasons = status_rebuild_reasons(answer, changes) if include_reasons else {}
        return ForYouSnapshot((), (UnavailableSource("For you", str(error)),), answer.machines, answer.refresh_due,
                              answer.comparison_deadline, reasons)


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
    """Read the rebuild machines, reasons, and other count from the console host."""
    fields = _console_count_payload()
    machines = fields.get("rebuild_machines")
    supplied = cast(list[object], machines) if isinstance(machines, list) else []
    rebuilds = tuple(machine for machine in rebuild_row.MACHINES if machine in supplied)
    reasons: dict[str, str] = {}
    supplied_reasons = fields.get("rebuild_reasons")
    if isinstance(supplied_reasons, dict):
        recorded = cast(dict[str, object], supplied_reasons)
        for machine in rebuilds:
            reason = recorded.get(machine)
            if isinstance(reason, str) and reason.strip():
                reasons[machine] = rebuild_row.short_reason(reason)
    partial = fields.get("partial") is True
    if fields.get("available") is True or partial:
        if "other_count" not in fields and "rebuild_machines" not in fields:
            count = fields.get("count")
        else:
            count = fields.get("other_count")
        if type(count) is int and count >= 0:
            return StatusLineActions(rebuilds, count, partial=partial, rebuild_reasons=reasons)
    return StatusLineActions(rebuilds, rebuild_reasons=reasons)


def action_count(source: str, rebuild: rebuild_row.RebuildAnswer | None = None) -> str:
    """Read the action count from its selected source."""
    return console_action_count() if source == "--for-you-console" else global_action_count(rebuild)

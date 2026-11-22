"""Admission socket client, with bounded queries and replay-safe acquisitions."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
from collections import deque
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum, auto
from importlib import import_module
import errno
import json
import math
import os
from pathlib import Path
from selectors import DefaultSelector, EVENT_READ, EVENT_WRITE, SelectorKey
import socket
import shlex
import subprocess
import sys
from types import TracebackType
from typing import TYPE_CHECKING, Callable, Protocol, cast
import uuid

from scripts.build_queue import execution_identity
from scripts.build_queue import admission_code, placement, snapshot_codec
from scripts.build_queue import admission_wire as status_wire
from scripts.build_queue.admission_wire import read_assignment as read_assignment
from scripts.build_queue.admission_wire import write_assignment as write_assignment
from scripts.build_targets import target_config
from scripts.direct import step_selection
if TYPE_CHECKING:
    from scripts.build_queue import admission_protocol as wire, admission_records as records
else:
    from scripts.build_queue import admission_wire as wire
from scripts.build_queue.system_clock import SystemClock

from scripts.lib import conf
from scripts.lib.atomic_file import write_text_atomic

BUILDS_SESSION = "builds"
LAUNCH_WAIT_LIMIT_REASON = "launch wait limit reached without admission"
LOADED_ADMISSION_CODE_VERSION = os.environ.get("BUILDLOG_ADMISSION_CODE_VERSION") or admission_code.admission_code_version()


class ClientClock(Protocol):
    def now(self) -> float: ...
    def monotonic(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


class PlacementConstraintBuilder(Protocol):
    """Read fixed caller policy without importing the provider command cycle."""

    def __call__(self, repo_root: Path, package: str,
                 kind: placement.CommandKind,
                 command: tuple[str, ...], session: placement.SubmittingSession,
                 source_key: execution_identity.SourceIdentity, routing: target_config.Routing,
                 selection: step_selection.TargetSelection, *, writes_tree: bool) -> placement.PlacementConstraints: ...


CLOCK: ClientClock = SystemClock()


class TransportUnavailable(ConnectionError):
    pass


class CheckerFailure(ValueError):
    pass


class _QueueUnreachable(Exception):
    pass


def _whole_seconds(key: str, fallback: int) -> int:
    raw = os.environ.get(key, str(fallback))
    return int(raw) if raw.isdecimal() else fallback


def _connect(timeout: float) -> socket.socket:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.settimeout(timeout)
        connection.connect(str(wire.admission_socket_path()))
    except BaseException:
        connection.close()
        raise
    return connection


def _remaining_seconds(deadline: float) -> float:
    left = deadline - CLOCK.monotonic()
    if left <= 0:
        raise TimeoutError("no answer")
    return left


def _line(connection: socket.socket, pending: bytearray, deadline: float) -> bytes:
    while b"\n" not in pending:
        connection.settimeout(_remaining_seconds(deadline))
        chunk = connection.recv(65536)
        if not chunk:
            raise TransportUnavailable("closed connection")
        pending.extend(chunk)
    return _take_line(pending)


def _take_line(pending: bytearray) -> bytes:
    end = pending.index(b"\n")
    line = bytes(pending[:end])
    del pending[:end + 1]
    return line


def _parse_acquisition_reply(line: bytes, request: wire.Request) -> wire.Reply:
    try:
        with snapshot_codec.snapshot_read_policy(snapshot_codec.SnapshotReadPolicy.ACQUISITION_FIELDS):
            return wire.parse_reply(line, request)
    except ValueError as error:
        raise CheckerFailure(str(error)) from error


class AcquisitionDeadline(Enum):
    LAUNCH = auto()
    UNREACHABLE = auto()


type DeadlineStop = AcquisitionDeadline | CheckerFailure
type CheckDeadline = Callable[[], DeadlineStop | None]
type AdmissionRequest = wire.AdmitRequest | wire.LaunchRequest | wire.ServiceEstimateRequest
type AdmissionReply = wire.Admitted | wire.LaunchAdmitted | wire.Refused
type CallReply = AdmissionReply | wire.Ok | wire.BuildHistoryReply | LeftQueue | wire.Assignment | wire.LocalPlacementGranted | wire.PlacementWait | wire.DispatchStatus | wire.FirstAuthorization | wire.RepositorySlotReady | wire.RepositorySlotExpired | status_wire.PlacementStatusReply


@dataclass
class AdmissionStream:
    pending: bytearray = field(default_factory=bytearray)
    replies: deque[wire.Reply | CheckerFailure] = field(default_factory=deque)
    connecting: bool = False
    received_wait: bool = False
    history_lookup: int | None = None
    outgoing: bytes = b""
    error: OSError | None = None


def _acquisition_socket() -> socket.socket:
    return socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)


class AcquisitionSockets:
    def __init__(self, selector: DefaultSelector, reservations: dict[str, wire.Admitted],
                 record_failure: Callable[[BaseException, str], None]) -> None:
        self.selector: DefaultSelector = selector
        self.record_failure: Callable[[BaseException, str], None] = record_failure
        self.streams: dict[socket.socket, AdmissionStream] = {}
        self.owner: socket.socket | None = None
        self.reservations: dict[str, wire.Admitted] = reservations

    def open(self, request: AdmissionRequest, deadline: CheckDeadline) -> DeadlineStop | None:
        # A failed replacement cannot restore the old socket's deadline authority.
        self.owner = None
        if (expired := deadline()) is not None:
            return expired
        connection: socket.socket | None = None
        failure: BaseException | None = None
        try:
            connection = _acquisition_socket()
            connection.setblocking(False)
            path = str(wire.admission_socket_path())
            if (expired := deadline()) is not None:
                return expired
            result = connection.connect_ex(path)
            if result not in (0, errno.EISCONN, errno.EINPROGRESS, errno.EALREADY, errno.EINTR):
                raise OSError(result, os.strerror(result))
            outgoing = wire.encode_request(request).encode()
            if (expired := deadline()) is not None:
                return expired
            stream = AdmissionStream(connecting=result not in (0, errno.EISCONN), outgoing=outgoing)
            _ = self.selector.register(connection, EVENT_READ | EVENT_WRITE)
            self.streams[connection] = stream
            self.owner = connection
        except BaseException as error:
            failure = error
            raise
        finally:
            if connection is not None and connection not in self.streams:
                try:
                    connection.close()
                except BaseException as error:
                    if isinstance(failure, KeyboardInterrupt) and isinstance(error, KeyboardInterrupt):
                        self.record_failure(failure, "socket setup")
                    if failure is None or isinstance(error, KeyboardInterrupt):
                        raise
                    failure.add_note(f"socket close failed: {error}")
        return None

    def fail(self, connection: socket.socket, stream: AdmissionStream, error: OSError) -> None:
        _ = self.selector.unregister(connection)
        stream.error = error

    def prune(self) -> list[tuple[socket.socket, OSError]]:
        failed: list[tuple[socket.socket, OSError]] = []
        for connection, stream in tuple(self.streams.items()):
            if stream.error is not None and not stream.replies:
                failed.append((connection, stream.error))
                connection.close()
                del self.streams[connection]
        return failed

    def close(self, cleanup: AdmissionResources) -> None:
        for connection, stream in self.streams.items():
            if stream.error is None:
                cleanup.run(lambda: self.selector.unregister(connection))
            cleanup.run(connection.close)
        self.streams.clear()
        cleanup.run(self.selector.close)

    def send(self, connection: socket.socket, request: wire.Request,
             deadline: CheckDeadline) -> DeadlineStop | None:
        stream = self.streams[connection]
        assert not stream.outgoing
        outgoing = wire.encode_request(request).encode()
        if (expired := deadline()) is not None:
            return expired
        if isinstance(request, (wire.AdmitRequest, wire.LaunchRequest, wire.ServiceEstimateRequest)):
            stream.received_wait = False
        stream.outgoing = outgoing
        _ = self.selector.modify(connection, EVENT_READ | EVENT_WRITE)
        return None

    def terminal(self) -> AdmissionReply | None:
        refusal: wire.Refused | None = None
        for stream in reversed(self.streams.values()):
            for reply in stream.replies:
                if isinstance(reply, (wire.Admitted, wire.LaunchAdmitted)):
                    return reply
                if refusal is None and isinstance(reply, wire.Refused):
                    refusal = reply
        return refusal

    def next_reply(self) -> tuple[socket.socket, wire.Reply | CheckerFailure] | None:
        for connection, stream in reversed(self.streams.items()):
            if stream.replies:
                return connection, stream.replies[0]
        return None

    def read_ready(self, ready: list[tuple[SelectorKey, int]], request: AdmissionRequest) -> bool:
        received = False
        for key, mask in ready:
            connection = cast(socket.socket, key.fileobj)
            stream = self.streams[connection]
            if mask & EVENT_READ and not stream.connecting and self.receive(connection, stream, request):
                received = True
        return received

    def drain(self, request: AdmissionRequest) -> None:
        while self.read_ready(self.selector.select(0), request):
            pass

    def poll(self, request: AdmissionRequest, timeout: float, deadline: CheckDeadline) -> None:
        ready = self.selector.select(timeout)
        _ = self.read_ready(ready, request)
        if self.terminal() is not None or deadline() is not None:
            return
        for key, mask in ready:
            connection = cast(socket.socket, key.fileobj)
            stream = self.streams[connection]
            if mask & EVENT_WRITE and stream.error is None:
                if self.write(connection, stream, request, deadline) is not None:
                    return

    def write(self, connection: socket.socket, stream: AdmissionStream, request: AdmissionRequest,
              deadline: CheckDeadline) -> DeadlineStop | None:
        try:
            if stream.connecting:
                error = connection.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                if error:
                    raise OSError(error, os.strerror(error))
                stream.connecting = False
            if (expired := deadline()) is not None:
                return expired
            stream.outgoing = stream.outgoing[connection.send(stream.outgoing):]
            if not stream.outgoing:
                _ = self.selector.modify(connection, EVENT_READ)
        except BlockingIOError:
            pass
        except OSError as error:
            if not stream.connecting:
                self.drain(request)
            if stream.error is None:
                self.fail(connection, stream, error)
        return None

    def receive(self, connection: socket.socket, stream: AdmissionStream, request: AdmissionRequest) -> bool:
        try:
            chunk = connection.recv(65536)
            if not chunk:
                raise TransportUnavailable("closed connection")
        except (BlockingIOError, TimeoutError):
            return False
        except OSError as error:
            self.fail(connection, stream, error)
            return False
        stream.pending.extend(chunk)
        while b"\n" in stream.pending:
            try:
                reply = _parse_acquisition_reply(_take_line(stream.pending), request)
                if isinstance(reply, wire.Admitted):
                    _ = self.reservations.setdefault(reply.reservation, reply)
                if isinstance(reply, wire.Wait):
                    stream.received_wait = True
                stream.replies.append(reply)
            except CheckerFailure as error:
                stream.replies.append(error)
        return True


class AdmissionResources:
    def __init__(self, selector: DefaultSelector) -> None:
        self.reservations: dict[str, wire.Admitted] = {}
        self.sockets: AcquisitionSockets = AcquisitionSockets(selector, self.reservations, self.failed)
        self.keep: wire.Admitted | wire.LaunchAdmitted | None = None
        self.abort: BaseException | None = None
        self.interrupted: bool = False
        self.refusal: wire.Refused | None = None

    def __enter__(self) -> AdmissionResources:
        return self

    def failed(self, error: BaseException, operation: str) -> None:
        if isinstance(error, KeyboardInterrupt):
            if self.interrupted:
                raise error
            self.interrupted = True
        if self.abort is None:
            self.abort = error
        else:
            self.abort.add_note(f"{operation} failed: {error}")

    def run(self, operation: Callable[[], object]) -> None:
        try:
            _ = operation()
        except BaseException as error:
            self.failed(error, "socket cleanup")

    def release(self, grant: wire.Admitted) -> None:
        try:
            reply = release(grant.reservation, 1, grant.cleanup_timing)
        except BaseException as error:
            self.failed(error, f"release of {grant.reservation}")
        else:
            if isinstance(reply, wire.Refused):
                if self.abort is not None:
                    self.abort.add_note(f"release of {grant.reservation} refused: {reply.reason}")
                if self.refusal is None:
                    self.refusal = reply

    def __exit__(self, _kind: type[BaseException] | None, error: BaseException | None,
                 _traceback: TracebackType | None) -> None:
        if error is not None:
            self.failed(error, "admission completion")
        self.sockets.close(self)
        keep = self.keep if isinstance(self.keep, wire.Admitted) else None
        for grant in self.reservations.values():
            if keep is None or grant.reservation != keep.reservation:
                self.release(grant)
        if keep is not None and (self.abort is not None or self.refusal is not None):
            self.release(keep)
        if self.abort is not None:
            raise self.abort


def _is_placement_control(request: wire.Request) -> bool:
    return isinstance(request, (wire.ClaimFailure, wire.ConsumePermission, wire.ExecutionReport, wire.CancelPlacement))


def _waits_for_live_service(request: wire.Request) -> bool:
    """A placement call waits while a live service is slow, and fails at once when it is down."""
    return _is_placement_control(request) or isinstance(request, status_wire.PlacementStatusRequest)


def _query(request: wire.ViewRequest | wire.ExplainRequest | wire.MineRequest | wire.PlacementRequest
           | wire.ClaimFailure | wire.ConsumePermission | wire.ExecutionReport | wire.PrerequisiteReport
           | wire.CancelPlacement | status_wire.PlacementStatusRequest, *, wait_for_service: bool = True) -> wire.Reply:
    from scripts.build_queue import admission_protocol as wire, admission_records as records
    if wait_for_service and _waits_for_live_service(request):
        reply = _call(request, lambda _wait: None)
        if isinstance(reply, LeftQueue):
            raise _QueueLeft()
        if isinstance(reply, wire.Refused):
            raise wire.AdmissionUnavailable(reply.reason)
        return reply
    request = replace(request, code_version=LOADED_ADMISSION_CODE_VERSION)
    timeout = records.read_memory_config().query_timeout_seconds
    deadline = CLOCK.monotonic() + timeout
    try:
        with _connect(_remaining_seconds(deadline)) as connection:
            outgoing = wire.encode_request(request).encode()
            connection.settimeout(_remaining_seconds(deadline))
            connection.sendall(outgoing)
            line = _line(connection, bytearray(), deadline)
            reply = wire.parse_reply(line, request)
            if isinstance(reply, wire.Refused):
                raise wire.AdmissionUnavailable(reply.reason)
            return reply
    except FileNotFoundError as error:
        raise wire.AdmissionUnavailable("no socket") from error
    except ConnectionRefusedError as error:
        raise wire.AdmissionUnavailable("connect refused") from error
    except TimeoutError as error:
        raise wire.AdmissionUnavailable(f"no answer in {timeout:g} s") from error
    except (OSError, ValueError) as error:
        raise wire.AdmissionUnavailable(str(error) or "invalid answer") from error


def placement_status(episode: str, attempt: str, *, wait_for_service: bool = True) -> status_wire.PlacementStatusReply:
    """Read a saved attempt without requesting authorization."""
    reply = _query(status_wire.PlacementStatusRequest(episode, attempt), wait_for_service=wait_for_service)
    if isinstance(reply, status_wire.PlacementStatusReply):
        return reply
    raise wire.AdmissionUnavailable("invalid placement status answer")


def _print_placement_status(episode: str, attempt: str) -> int:
    status = placement_status(episode, attempt).status
    if isinstance(status, status_wire.UnknownPlacementStatus):
        print("unknown\t-\t-\t-\t-\t-")
        return 1
    from scripts.build_hold.owning_session import OwningSession

    assignment = status.assignment
    current = isinstance(status.state, placement.CurrentAssignment)
    owner = assignment.owner
    fields = ("current" if current else "closed", assignment.destination.target,
        f"{owner.host}:{owner.pid}:{owner.start_time}",
        assignment.session.session_id if isinstance(assignment.session, OwningSession) else "-",
        status.claim.claim_id if isinstance(status.claim, placement.ProviderClaimReference) else "-",
        status.state.reason if isinstance(status.state, placement.ClosedAssignment) else "-")
    print("\t".join(value.replace("\t", " ").replace("\n", " ").replace("\r", " ") for value in fields))
    return 0 if current else 1


def request_placement(request: wire.PlacementRequest, *, report_refusal: bool = True
                      ) -> wire.Assignment | wire.LocalPlacementGranted | wire.PlacementWait | wire.DispatchStatus:
    """Request a durable destination without selecting a fallback in the caller."""
    reply = _call(request, lambda _wait: None, report_refusal=report_refusal)
    if isinstance(reply, LeftQueue):
        raise _QueueLeft()
    if isinstance(reply, wire.Refused):
        raise wire.AdmissionUnavailable(reply.reason)
    if isinstance(reply, (wire.Assignment, wire.LocalPlacementGranted, wire.PlacementWait, wire.DispatchStatus)):
        return reply
    raise wire.AdmissionUnavailable("invalid placement answer")


def acquire_placement(request: wire.PlacementRequest
                      ) -> wire.Assignment | wire.LocalPlacementGranted | wire.PlacementWait | wire.DispatchStatus | LeftQueue:
    """Return cancellation as an outcome for callers outside the command parser."""
    try:
        return request_placement(request)
    except _QueueLeft:
        return LeftQueue()


def report_prerequisite(request: wire.PrerequisiteReport, *, report_refusal: bool = True) -> wire.Ok:
    reply = _call(request, lambda _wait: None, report_refusal=report_refusal)
    if isinstance(reply, LeftQueue):
        raise _QueueLeft()
    if isinstance(reply, wire.Ok):
        return reply
    if not report_refusal and isinstance(reply, wire.Refused):
        raise wire.AdmissionUnavailable(reply.reason)
    raise wire.AdmissionUnavailable("invalid prerequisite answer")


def cancel_placement(request: wire.CancelPlacement) -> wire.Ok:
    reply = _query(request)
    if isinstance(reply, wire.Ok):
        return reply
    raise wire.AdmissionUnavailable("invalid cancellation answer")


@dataclass(frozen=True)
class KeepPlacementPrerequisite:
    pass


KEEP_PLACEMENT_PREREQUISITE = KeepPlacementPrerequisite()
# "after-grant" is the caller that takes its cargo token after a local grant, so the service
# needs no token before that grant. "required" is the older caller that holds the token
# before its grant; the service gives it no local grant until it reports the token held.
# Both forms use the existing wire values, so any running service reads them.
TOKEN_REQUIRED_CHOICES = ("after-grant", "required")


def token_prerequisite(choice: str) -> placement.TokenRequired | placement.TokenNotRequired:
    return placement.TokenNotRequired() if choice == "after-grant" else placement.TokenRequired()


def finish_step(path: Path, status: int, reason: str = "", *,
                prerequisite: placement.LocalExecutionPrerequisite | KeepPlacementPrerequisite = KEEP_PLACEMENT_PREREQUISITE
                ) -> wire.DispatchStatus:
    from scripts.build_queue import placement

    reply = read_assignment(path)
    if not isinstance(reply, wire.LocalPlacementGranted):
        raise wire.AdmissionUnavailable("finish-step requires a local grant")
    if not isinstance(prerequisite, KeepPlacementPrerequisite):
        _ = report_prerequisite(wire.PrerequisiteReport(uuid.uuid4().hex,
            reply.assignment.episode, reply.assignment.owner, prerequisite))
    _ = release(reply.grant.reservation, status, reply.grant.cleanup_timing)
    result = report_execution(wire.ExecutionReport(uuid.uuid4().hex, reply.assignment, placement.NoProviderClaim(),
        execution_identity.ExecutionNotStarted() if reason else execution_identity.ExecutionEvidenceUnavailable("the local command has ended", CLOCK.now()),
        placement.PrelaunchRefused(reason) if reason else placement.AttemptCompleted(status),
        placement.DispatchPathSettled()))
    if result.assignment != reply.assignment or result.state != placement.ClosedAssignment(reason or "completed"):
        raise wire.AdmissionUnavailable("the service did not confirm the end of the local attempt")
    return result


def claim_failure(request: wire.ClaimFailure) -> wire.DispatchStatus:
    reply = _query(request)
    if isinstance(reply, wire.DispatchStatus):
        return reply
    raise wire.AdmissionUnavailable("invalid claim failure answer")


def consume_permission(request: wire.ConsumePermission) -> wire.FirstAuthorization | wire.DispatchStatus:
    """A repeated call can return status but cannot restore a lost authorization."""
    reply = _query(request)
    if isinstance(reply, (wire.FirstAuthorization, wire.DispatchStatus)):
        return reply
    raise wire.AdmissionUnavailable("invalid permission answer")


def report_execution(request: wire.ExecutionReport) -> wire.DispatchStatus:
    reply = _query(request)
    if isinstance(reply, wire.DispatchStatus):
        return reply
    raise wire.AdmissionUnavailable("invalid execution report answer")


class _HistoryNeeded(BaseException):
    pass


@dataclass(frozen=True)
class HistoryPending:
    lookup_id: int
    connection: socket.socket
    deadline: float


@dataclass(frozen=True)
class HistoryDone:
    generation: records.BuildHistoryGeneration


@dataclass
class EstimateRefresh:
    estimated_at: float
    history: HistoryPending | HistoryDone | None = None


_ESTIMATE_REFRESH: ContextVar[EstimateRefresh | None] = ContextVar("estimate_refresh", default=None)


@dataclass(frozen=True)
class ReadConfigurationAtUse:
    """Read local admission configuration when a request needs it."""


READ_CONFIGURATION_AT_USE = ReadConfigurationAtUse()


def build_history_generation(configuration: records.MemoryConfig | ReadConfigurationAtUse = READ_CONFIGURATION_AT_USE) -> records.BuildHistoryGeneration:
    """Read the generation before reading build history."""
    from scripts.build_queue import admission_records as records
    refreshing = _ESTIMATE_REFRESH.get()
    if refreshing is not None:
        if not isinstance(refreshing.history, HistoryDone):
            raise _HistoryNeeded()
        return refreshing.history.generation
    reply = _call(wire.BuildHistoryRequest(), lambda _wait: None, configuration=configuration)
    if isinstance(reply, LeftQueue):
        raise _QueueLeft()
    if isinstance(reply, wire.Refused):
        raise CheckerFailure(reply.reason)
    if not isinstance(reply, wire.BuildHistoryReply) or reply.generation < 0:
        raise CheckerFailure("invalid build history generation answer")
    return records.BuildHistoryGeneration(reply.generation)


def view() -> records.QueueRead | wire.CloudQueueRead:
    from scripts.build_queue import admission_protocol as wire, admission_records as records
    reply = _query(wire.ViewRequest())
    if not isinstance(reply, wire.ViewReply):
        raise wire.AdmissionUnavailable("invalid view answer")
    records.warn_configuration_diagnostics(reply.reading.diagnostics)
    return reply.reading


def explain() -> records.QueueExplanation:
    from scripts.build_queue import admission_protocol as wire, admission_records as records
    reply = _query(wire.ExplainRequest())
    if not isinstance(reply, wire.ExplainReply):
        raise wire.AdmissionUnavailable("invalid explanation answer")
    records.warn_configuration_diagnostics(reply.diagnostics)
    return reply.explanation


def mine(session_id: str) -> records.QueueRead | wire.CloudQueueRead:
    from scripts.build_queue import admission_protocol as wire, admission_records as records
    reply = _query(wire.MineRequest(session_id))
    if not isinstance(reply, wire.MineReply):
        raise wire.AdmissionUnavailable("invalid step answer")
    records.warn_configuration_diagnostics(reply.reading.diagnostics)
    return reply.reading


def _refreshed(first: wire.AdmitRequest | wire.LaunchRequest,
               refresh: Callable[[], wire.AdmitRequest | wire.LaunchRequest], *,
               refreshing: EstimateRefresh | None = None
               ) -> wire.AdmitRequest | wire.LaunchRequest:
    token = _ESTIMATE_REFRESH.set(refreshing)
    try:
        updated = refresh()
    finally:
        _ESTIMATE_REFRESH.reset(token)
    # A refresh changes estimates only, never the step's identity or place.
    if isinstance(first, wire.AdmitRequest) and isinstance(updated, wire.AdmitRequest):
        if isinstance(first.place, wire.CloudPlace):
            return first
        return replace(first, profile=updated.profile, fresh_test=updated.fresh_test)
    if isinstance(first, wire.LaunchRequest) and isinstance(updated, wire.LaunchRequest):
        return replace(first, profile=updated.profile)
    raise CheckerFailure("refresh changed request kind")


@dataclass(frozen=True)
class LeftQueue:
    pass


class _QueueLeft(Exception):
    pass


@dataclass
class AcquisitionRetry:
    deadline: float
    launch_without_wait: bool = False
    next_leave: float = float("inf")
    last_outage_line: float = float("-inf")
    leave_interval: float = float("inf")
    unreachable_deadline: float = float("inf")

    def start_leave_schedule(self, interval: float) -> None:
        self.leave_interval = interval
        if self.next_leave == float("inf") and (
                os.environ.get("LINT_QUEUE_LEAVE_HOOK") or os.environ.get("BUILDLOG_MAC_IDLE_HOOK")):
            self.next_leave = CLOCK.monotonic() + self.leave_interval

    def left_queue(self) -> bool:
        if CLOCK.monotonic() < self.next_leave:
            return False
        left = _leave_queue()
        self.next_leave = CLOCK.monotonic() + self.leave_interval
        return left

    def expired(self, checker: CheckerRetry | None = None, *,
                checker_due: bool = False) -> DeadlineStop | None:
        now = CLOCK.monotonic()
        if now >= self.deadline:
            return AcquisitionDeadline.LAUNCH
        if now >= self.unreachable_deadline:
            return AcquisitionDeadline.UNREACHABLE
        if checker is not None and (now > checker.deadline or now == checker.deadline and not checker_due):
            return CheckerFailure(checker.reason)
        return None

    def bound(self, seconds: float) -> float:
        return max(0.0, min(seconds, self.deadline - CLOCK.monotonic(),
                            self.next_leave - CLOCK.monotonic(), self.unreachable_deadline - CLOCK.monotonic()))


_ACQUISITION_RETRY: ContextVar[AcquisitionRetry | None] = ContextVar("acquisition_retry", default=None)


@contextmanager
def placement_acquisition() -> Generator[None]:
    """Preserve one leave schedule across placement replies and estimate requests."""
    token = _ACQUISITION_RETRY.set(AcquisitionRetry(float("inf")))
    try:
        yield
    except _QueueUnreachable as error:
        raise wire.AdmissionUnavailable(str(error)) from error
    finally:
        _ACQUISITION_RETRY.reset(token)


def _no_attempt_hook() -> None:
    pass


@dataclass(frozen=True)
class AdmissionHoldCheck:
    wait_until_free: Callable[[], None]
    admits: Callable[[], bool]

    def __call__(self) -> None:
        self.wait_until_free()


def _leave_queue() -> bool:
    hook = os.environ.get("LINT_QUEUE_LEAVE_HOOK") or os.environ.get("BUILDLOG_MAC_IDLE_HOOK")
    if not hook:
        return False
    retry = _ACQUISITION_RETRY.get()
    deadline = retry.unreachable_deadline if retry is not None else math.inf
    remaining = deadline - CLOCK.monotonic()
    if remaining <= 0:
        return False
    try:
        return subprocess.run(["/bin/sh", "-c", hook], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            timeout=None if remaining == math.inf else remaining).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def _refused(reason: str, *, checker: bool = False) -> wire.Refused:
    text = f"memory admission check failed ({reason}); step refused" if checker else reason
    print(text, file=sys.stderr)
    return wire.Refused(reason)


def nested_reservation_reason() -> str:
    """Refuse a child only when the same queue still holds its live parent's reservation.

    The holder must match by pid and start time, so a reused pid never refuses a step.
    """
    marker = os.environ.get("BUILDLOG_RESERVED_STEP", "")
    if not marker:
        return ""
    from scripts.build_queue import admission_records as records
    from scripts.buildlog import store

    try:
        reservation, raw_pid, directory = marker.split("\t")
        pid = int(raw_pid)
        if not reservation or pid <= 0 or Path(directory).resolve() != (store.root() / "admission").resolve():
            return ""
        start = records.process_start_time(pid)
        if start is None:
            return ""
        state = cast(object, json.loads((Path(directory) / "state.json").read_text()))
    except (OSError, ValueError):
        return ""
    if not isinstance(state, dict):
        return ""
    reservations = cast(dict[str, object], state).get("reservations")
    if not isinstance(reservations, dict):
        return ""
    for value in cast(dict[str, object], reservations).values():
        if not isinstance(value, dict):
            continue
        row = cast(dict[str, object], value)
        owner = row.get("owner")
        if (row.get("reservation_id") == reservation and isinstance(owner, dict)
                and cast(dict[str, object], owner).get("pid") == pid
                and cast(dict[str, object], owner).get("start_time") == start):
            return (f"verify step cannot run inside reserved step {reservation} (pid {pid}); "
                    + "run it outside the outer step")
    return ""


class AdmissionRefusalHandler(Protocol):
    def __call__(self, reason: str, *, checker: bool = False) -> wire.Refused: ...


def _call(request: wire.Request, on_wait: Callable[[wire.Wait], None],
          refresh: Callable[[], wire.AdmitRequest | wire.LaunchRequest] | None = None,
          *, attempt: Callable[[], None] = _no_attempt_hook, report_refusal: bool = True,
          configuration: records.MemoryConfig | wire.AcquisitionTiming | wire.CleanupTiming | ReadConfigurationAtUse = READ_CONFIGURATION_AT_USE
          ) -> wire.Admitted | wire.LaunchAdmitted | wire.Refused | wire.Ok | wire.BuildHistoryReply | LeftQueue | wire.Assignment | wire.LocalPlacementGranted | wire.PlacementWait | wire.DispatchStatus | wire.FirstAuthorization | wire.RepositorySlotReady | wire.RepositorySlotExpired | status_wire.PlacementStatusReply:
    def refuse(reason: str, *, checker: bool = False) -> wire.Refused:
        return _refused(reason, checker=checker) if report_refusal else wire.Refused(reason)

    if isinstance(request, (wire.AdmitRequest, wire.ServiceEstimateRequest, wire.LaunchRequest,
                            wire.PlacementRequest, wire.VerifyRunSlotRequest)):
        reason = nested_reservation_reason()
        if reason:
            return refuse(reason)
    acquisition = _is_placement_control(request) or isinstance(request, (wire.AdmitRequest, wire.ServiceEstimateRequest, wire.LaunchRequest,
        wire.BuildHistoryRequest, wire.PlacementRequest, wire.PrerequisiteReport, wire.RepositorySlotRequest))
    retry = _ACQUISITION_RETRY.get() if acquisition else None
    owns_retry = retry is None
    if retry is None:
        limit = _whole_seconds("BUILDLOG_MEM_WAIT_LIMIT_S", 900)
        launch = isinstance(request, wire.LaunchRequest)
        deadline = (CLOCK.monotonic() + max(0.0, request.deadline - CLOCK.now())
                    if isinstance(request, wire.RepositorySlotRequest) else
                    CLOCK.monotonic() + limit if launch and limit > 0 else float("inf"))
        retry = AcquisitionRetry(deadline, launch_without_wait=launch and limit == 0)
    token = _ACQUISITION_RETRY.set(retry)
    try:
        from scripts.build_queue import admission_records as records
        if isinstance(configuration, ReadConfigurationAtUse):
            try:
                resolved = records.read_memory_config(report=False)
            except conf.Unusable as error:
                return refuse(str(error), checker=True)
        else:
            resolved = configuration
        request = replace(request, code_version=LOADED_ADMISSION_CODE_VERSION)
        if report_refusal and isinstance(resolved, records.MemoryConfig):
            records.warn_configuration_diagnostics(resolved.diagnostics)
        if isinstance(request, (wire.AdmitRequest, wire.LaunchRequest, wire.ServiceEstimateRequest)):
            assert not isinstance(resolved, wire.CleanupTiming)
            reply = _run_admission(request, on_wait, refresh, attempt, retry, resolved, refuse)
        else:
            reply = _control_reply(request, retry, resolved, refuse)
        if not report_refusal and not isinstance(reply, wire.Refused) and isinstance(resolved, records.MemoryConfig):
            records.warn_configuration_diagnostics(resolved.diagnostics)
        return reply
    except _QueueLeft:
        return LeftQueue()
    except _QueueUnreachable as error:
        if not owns_retry:
            raise
        return refuse(str(error))
    except conf.Unusable as error:
        if report_refusal:
            raise
        return refuse(str(error), checker=True)
    finally:
        _ACQUISITION_RETRY.reset(token)


def _unreachable(configuration: records.MemoryConfig | wire.AcquisitionTiming) -> _QueueUnreachable:
    duration = _unreachable_duration(configuration.unreachable_limit_seconds)
    return _QueueUnreachable(f"build queue unreachable for {duration}; step stopped; cargo token given back")


def _begin_unreachable(retry: AcquisitionRetry, configuration: records.MemoryConfig | wire.AcquisitionTiming) -> None:
    if retry.unreachable_deadline == float("inf"):
        retry.unreachable_deadline = CLOCK.monotonic() + configuration.unreachable_limit_seconds
        duration = _unreachable_duration(configuration.unreachable_limit_seconds)
        print(f"build queue not reachable; trying for {duration}", file=sys.stderr)


@dataclass
class ReconnectNotice:
    pending: bool = False
    restart_reported: bool = False

    def disconnected(self, *, restarted: bool = False) -> None:
        self.pending = True
        if restarted and not self.restart_reported:
            print("admission service restarted; waiting again", file=sys.stderr)
            self.restart_reported = True

    def reached(self, reply: wire.Reply) -> None:
        if not self.pending:
            return
        self.pending = False
        reason = " ".join(_reconnect_wait_reason(reply).splitlines())
        print("queue reached again" + (f": {reason}" if reason else ""), file=sys.stderr, flush=True)


def _reconnect_wait_reason(reply: wire.Reply) -> str:
    from scripts.build_queue import admission_records as records

    wait = reply.wait if isinstance(reply, wire.PlacementWait) else reply
    if isinstance(wait, (wire.Wait, wire.RepositorySlotWaiting)):
        return wait.text
    if isinstance(wait, wire.VerifyRunSlotWaiting):
        return records.run_limit_text(wait.reason)
    if isinstance(wait, wire.PlacementResourceWait):
        return f"waiting for {wait.resource}: {wait.reason}"
    if isinstance(wait, wire.PlacementEvidenceWait):
        return "waiting for host evidence"
    return ""


def _transport_failure(error: OSError, retry: AcquisitionRetry,
                       configuration: records.MemoryConfig | wire.AcquisitionTiming | wire.CleanupTiming,
                       *, bound_outage: bool) -> bool:
    restarted = isinstance(error, TransportUnavailable) or error.errno in (errno.ECONNRESET, errno.EPIPE)
    if bound_outage and not isinstance(configuration, wire.CleanupTiming) and (
            restarted or isinstance(error, (FileNotFoundError, ConnectionRefusedError))):
        _begin_unreachable(retry, configuration)
    if not restarted:
        interval = (configuration.outage_notice_seconds if not isinstance(configuration, (wire.AcquisitionTiming, wire.CleanupTiming))
                    else configuration.silence_seconds)
        if CLOCK.monotonic() - retry.last_outage_line >= interval:
            reason = "no socket" if isinstance(error, FileNotFoundError) else "connect refused" if isinstance(error, ConnectionRefusedError) else str(error)
            print(f"admission service unavailable: {reason}; waiting again", file=sys.stderr)
            retry.last_outage_line = CLOCK.monotonic()
    return restarted


def _control_reply(request: wire.Request, retry: AcquisitionRetry,
                   configuration: records.MemoryConfig | wire.AcquisitionTiming | wire.CleanupTiming,
                   refuse: AdmissionRefusalHandler) -> CallReply:
    from scripts.build_queue import admission_records as records

    placement_control = _waits_for_live_service(request)
    generation_query = isinstance(request, wire.BuildHistoryRequest)
    slot_waiting = (wire.RepositorySlotWaiting(request.repository, tuple(range(1, request.maximum + 1)),
                    request.maximum, f"slots.{request.machine}.{request.repository}.max")
                    if isinstance(request, wire.RepositorySlotRequest) else None)
    bound_outage = isinstance(request, (wire.BuildHistoryRequest, wire.PlacementRequest, wire.PrerequisiteReport,
                                        wire.RepositorySlotRequest, wire.VerifyRunSlotRequest))
    if bound_outage and not isinstance(request, wire.VerifyRunSlotRequest):
        retry.start_leave_schedule(configuration.running_pass_seconds if isinstance(configuration, records.MemoryConfig)
                                   else configuration.reconnect_seconds)
    if not isinstance(configuration, wire.CleanupTiming) and (
            isinstance(configuration, wire.AcquisitionTiming) or placement_control):
        if retry.unreachable_deadline == float("inf"):
            retry.unreachable_deadline = CLOCK.monotonic() + configuration.unreachable_limit_seconds

    def deadline_reply(expired: DeadlineStop = AcquisitionDeadline.LAUNCH) -> wire.Refused | wire.RepositorySlotExpired:
        if expired is AcquisitionDeadline.UNREACHABLE:
            assert not isinstance(configuration, wire.CleanupTiming)
            raise _unreachable(configuration)
        if slot_waiting is not None:
            return wire.RepositorySlotExpired(slot_waiting.expired_text)
        return wire.Refused(LAUNCH_WAIT_LIMIT_REASON) if generation_query else refuse(LAUNCH_WAIT_LIMIT_REASON, checker=True)

    last_valid = last_notice = CLOCK.monotonic()
    reconnect = ReconnectNotice()
    showed_slot = False
    verify_wait_text = ""
    while True:
        if retry.left_queue():
            return LeftQueue()
        if (expired := retry.expired()) is not None:
            return deadline_reply(expired)
        try:
            interval = min(configuration.silence_seconds, configuration.reconnect_seconds) if generation_query else configuration.silence_seconds
            timeout = retry.bound(interval)
            with _connect(timeout) as connection:
                outgoing = wire.encode_request(request).encode()
                send_timeout = retry.bound(timeout)
                if (expired := retry.expired()) is not None:
                    return deadline_reply(expired)
                if send_timeout < timeout:
                    connection.settimeout(send_timeout)
                connection.sendall(outgoing)
                pending = bytearray()
                while True:
                    deadline = max(last_valid, last_notice) + configuration.silence_seconds
                    if generation_query:
                        deadline = min(deadline, CLOCK.monotonic() + configuration.reconnect_seconds)
                    try:
                        line = _line(connection, pending, min(deadline, retry.deadline, retry.next_leave,
                                                             retry.unreachable_deadline))
                    except TimeoutError:
                        if CLOCK.monotonic() >= retry.next_leave:
                            if retry.left_queue():
                                return LeftQueue()
                            if CLOCK.monotonic() < min(deadline, retry.deadline, retry.unreachable_deadline):
                                continue
                        if (expired := retry.expired()) is not None:
                            return deadline_reply(expired)
                        if generation_query:
                            raise
                        if retry.launch_without_wait:
                            return deadline_reply()
                        _silence_notice(last_valid)
                        last_notice = CLOCK.monotonic()
                        continue
                    reply = _parse_acquisition_reply(line, request)
                    if CLOCK.monotonic() > retry.deadline:
                        return deadline_reply()
                    reconnect.reached(reply)
                    retry.unreachable_deadline = (CLOCK.monotonic() + configuration.unreachable_limit_seconds
                        if isinstance(configuration, wire.AcquisitionTiming) else float("inf"))
                    if isinstance(reply, wire.Refused):
                        if generation_query or placement_control:
                            return reply
                        return refuse(reply.reason, checker=isinstance(request, wire.VerifyRunSlotRequest))
                    if isinstance(request, wire.VerifyRunSlotRequest):
                        if isinstance(reply, wire.Ok):
                            return reply
                        if not isinstance(reply, wire.VerifyRunSlotWaiting):
                            raise CheckerFailure("invalid Verify run slot answer")
                        text = records.run_limit_text(reply.reason)
                        if text != verify_wait_text:
                            print(text, file=sys.stderr, flush=True)
                            verify_wait_text = text
                        last_valid = CLOCK.monotonic()
                        continue
                    if slot_waiting is not None:
                        if isinstance(reply, (wire.RepositorySlotReady, wire.RepositorySlotExpired)):
                            return reply
                        if not isinstance(reply, wire.RepositorySlotWaiting):
                            raise CheckerFailure("invalid repository slot answer")
                        if reply != slot_waiting or not showed_slot:
                            print(reply.text, file=sys.stderr)
                        slot_waiting = reply
                        showed_slot = True
                        last_valid = CLOCK.monotonic()
                        continue
                    if isinstance(request, status_wire.PlacementStatusRequest):
                        if isinstance(reply, status_wire.PlacementStatusReply):
                            return reply
                        raise CheckerFailure("invalid placement status answer")
                    if placement_control:
                        if isinstance(reply, (wire.DispatchStatus, wire.FirstAuthorization, wire.Ok)):
                            return reply
                        raise CheckerFailure("invalid placement control answer")
                    if isinstance(request, wire.PlacementRequest):
                        if isinstance(reply, (wire.Assignment, wire.LocalPlacementGranted, wire.PlacementWait, wire.DispatchStatus)):
                            return reply
                        raise CheckerFailure("invalid placement answer")
                    if generation_query:
                        if retry.left_queue():
                            return LeftQueue()
                        if isinstance(reply, wire.BuildHistoryReply):
                            return reply
                        if isinstance(reply, wire.Wait):
                            continue
                        raise CheckerFailure("invalid build history generation answer")
                    if isinstance(reply, wire.Ok):
                        return reply
                    raise CheckerFailure("invalid admission answer")
        except CheckerFailure as error:
            reason = str(error) or "checker returned an error"
            return wire.Refused(reason) if generation_query else refuse(reason, checker=True)
        except OSError as error:
            if placement_control and isinstance(error, (FileNotFoundError, ConnectionRefusedError)):
                reason = "no socket" if isinstance(error, FileNotFoundError) else "connect refused"
                raise wire.AdmissionUnavailable(reason) from error
            if retry.launch_without_wait:
                return deadline_reply()
            if (expired := retry.expired()) is not None:
                return deadline_reply(expired)
            restarted = _transport_failure(error, retry, configuration, bound_outage=bound_outage)
            if restarted or bound_outage:
                reconnect.disconnected(restarted=restarted)
            if restarted:
                continue
            CLOCK.sleep(retry.bound(configuration.reconnect_seconds))


@dataclass
class CheckerRetry:
    reason: str = ""
    deadline: float = math.inf
    next_attempt: float = math.inf

    def fail(self, error: Exception, interval: float, limit: float) -> None:
        self.reason = str(error) or "checker returned an error"
        if self.deadline == math.inf:
            now = CLOCK.monotonic()
            self.deadline = now + limit
            self.next_attempt = now + interval

    def clear(self) -> None:
        self.deadline = self.next_attempt = math.inf

    def report(self, interval: float) -> None:
        self.next_attempt = CLOCK.monotonic() + interval
        print(f"memory admission check failed ({self.reason}); trying again, the step keeps its place", file=sys.stderr)


def _refresh_request(request: AdmissionRequest, refresh: Callable[[], wire.AdmitRequest | wire.LaunchRequest] | None,
                     refreshing: EstimateRefresh) -> AdmissionRequest:
    if isinstance(request, wire.ServiceEstimateRequest):
        return request
    if refresh is None:
        raise CheckerFailure("estimate cannot be refreshed")
    return replace(_refreshed(request, refresh, refreshing=refreshing), code_version=LOADED_ADMISSION_CODE_VERSION)


@dataclass(frozen=True)
class RetryAfterRelease:
    request: wire.AdmitRequest


type AdmissionCompletion = DeadlineStop | LeftQueue | _QueueUnreachable | None
type AdmissionResult = AdmissionReply | LeftQueue | RetryAfterRelease


def _run_admission(request: AdmissionRequest, on_wait: Callable[[wire.Wait], None],
                   refresh: Callable[[], wire.AdmitRequest | wire.LaunchRequest] | None,
                   attempt: Callable[[], None], retry: AcquisitionRetry,
                   configuration: records.MemoryConfig | wire.AcquisitionTiming,
                   refuse: AdmissionRefusalHandler) -> AdmissionReply | LeftQueue:
    while True:
        result = _acquire(request, on_wait, refresh, attempt, retry, configuration, refuse)
        if not isinstance(result, RetryAfterRelease):
            return result
        request = result.request


def _acquire(request: AdmissionRequest, on_wait: Callable[[wire.Wait], None],
             refresh: Callable[[], wire.AdmitRequest | wire.LaunchRequest] | None,
             attempt: Callable[[], None], retry: AcquisitionRetry,
             configuration: records.MemoryConfig | wire.AcquisitionTiming,
             refuse: AdmissionRefusalHandler) -> AdmissionResult:
    retry_request: wire.AdmitRequest | None = None
    cause: AdmissionCompletion = None
    checker = CheckerRetry()
    with AdmissionResources(DefaultSelector()) as resources:
        sockets = resources.sockets
        try:
            request, cause = _wait_for_admission_reply(
                sockets, request, on_wait, refresh, attempt, retry, checker, configuration)
        except _QueueLeft:
            cause = LeftQueue()
        except _QueueUnreachable as error:
            cause = error
        except BaseException as error:
            resources.failed(error, "acquisition")

        sockets.drain(request)
        answer = sockets.terminal()
        if isinstance(answer, (wire.Admitted, wire.LaunchAdmitted)) and not isinstance(cause, LeftQueue) and resources.abort is None:
            try:
                if isinstance(attempt, AdmissionHoldCheck):
                    if not attempt.admits() and isinstance(answer, wire.Admitted):
                        if not isinstance(request, wire.AdmitRequest):
                            raise CheckerFailure("grant does not match admission request")
                        retry_request = request
                else:
                    attempt()
            except _QueueLeft:
                cause = LeftQueue()
            else:
                if retry_request is None:
                    resources.keep = answer

    if resources.refusal is not None:
        return resources.refusal
    if resources.keep is not None:
        return resources.keep
    if retry_request is not None:
        if (expired := retry.expired(checker)) is not None:
            cause = expired
        if cause is None:
            return RetryAfterRelease(replace(retry_request, episode=uuid.uuid4().hex))
    if isinstance(cause, LeftQueue):
        return cause
    if isinstance(answer, wire.Refused):
        return refuse(answer.reason, checker=True)
    if cause is AcquisitionDeadline.LAUNCH:
        return refuse(LAUNCH_WAIT_LIMIT_REASON, checker=True)
    if cause is AcquisitionDeadline.UNREACHABLE:
        raise _unreachable(configuration)
    if isinstance(cause, _QueueUnreachable):
        raise cause
    assert isinstance(cause, CheckerFailure)
    return refuse(str(cause) or "checker returned an error", checker=True)


def _wait_for_admission_reply(sockets: AcquisitionSockets, initial_request: AdmissionRequest,
                              on_wait: Callable[[wire.Wait], None],
                              refresh: Callable[[], wire.AdmitRequest | wire.LaunchRequest] | None,
                              attempt: Callable[[], None], retry: AcquisitionRetry, checker: CheckerRetry,
                              configuration: records.MemoryConfig | wire.AcquisitionTiming
                              ) -> tuple[AdmissionRequest, AdmissionCompletion]:
    from scripts.build_queue import admission_records as records

    request: AdmissionRequest = initial_request
    interval = configuration.reconnect_seconds
    silence = configuration.silence_seconds
    retry.start_leave_schedule(configuration.running_pass_seconds if isinstance(configuration, records.MemoryConfig) else interval)
    failure_limit = _whole_seconds("BUILDLOG_MEM_CHECK_RETRY_S", 60)
    last_valid = last_notice = reconnect_at = CLOCK.monotonic()
    if isinstance(configuration, wire.AcquisitionTiming) and retry.unreachable_deadline == math.inf:
        retry.unreachable_deadline = last_valid + configuration.unreachable_limit_seconds
    had_wait = estimate_pending = False
    reconnect = ReconnectNotice()
    waiting_callback: tuple[socket.socket, wire.Wait] | None = None
    refreshing: EstimateRefresh | None = None
    refreshed_write: tuple[socket.socket, AdmissionRequest] | None = None
    answered_expiry = -math.inf
    lookup_id = 0
    history_request = wire.BuildHistoryRequest(code_version=LOADED_ADMISSION_CODE_VERSION)

    def fail_history(error: Exception) -> None:
        assert refreshing is not None and isinstance(refreshing.history, HistoryPending)
        # Keep the socket's lookup ID so a late reply cannot satisfy a new lookup.
        refreshing.history = None
        checker.fail(error, interval, failure_limit)

    def transport_failed(error: OSError) -> None:
        _begin_unreachable(retry, configuration)
        restarted = _transport_failure(error, retry, configuration, bound_outage=False)
        reconnect.disconnected(restarted=restarted)

    if (expired := retry.expired()) is not None:
        return request, expired
    if not isinstance(attempt, AdmissionHoldCheck):
        attempt()

    checker_due = False

    def deadline_limit() -> DeadlineStop | None:
        pending_check = waiting_callback is not None or (
            refreshing is not None and not isinstance(refreshing.history, HistoryPending))
        due = checker_due or pending_check and checker.next_attempt <= CLOCK.monotonic()
        return retry.expired(checker, checker_due=due)

    def check_failed(error: Exception) -> DeadlineStop | None:
        if (expired := deadline_limit()) is not None:
            return expired
        if not had_wait:
            return CheckerFailure(str(error))
        sockets.drain(request)
        if sockets.terminal() is None:
            checker.fail(error, interval, failure_limit)
        return None

    while True:
        checker_due = False
        if sockets.terminal() is None:
            sockets.drain(request)
        if refreshed_write is not None:
            written_peer, written_request = cast(tuple[socket.socket, AdmissionRequest], refreshed_write)
            stream = sockets.streams[written_peer]
            if not stream.outgoing:
                assert refreshing is not None
                answered_expiry = refreshing.estimated_at
                request = written_request
                refreshing = None
                refreshed_write = None
                checker.clear()
            elif stream.error is not None:
                refreshing = None
                refreshed_write = None

        if (answer := sockets.terminal()) is not None:
            # A wait that arrived in the same read as the answer is the first answer after a reconnect.
            owner_stream = sockets.streams.get(sockets.owner) if sockets.owner is not None else None
            replies = owner_stream.replies if owner_stream is not None else ()
            reconnect.reached(next((reply for reply in replies if isinstance(reply, wire.Wait)), answer))
            return request, None
        if retry.launch_without_wait and any(stream.error is not None for stream in sockets.streams.values()):
            return request, AcquisitionDeadline.LAUNCH

        now = CLOCK.monotonic()
        for peer, error in sockets.prune():
            if refreshing is not None and isinstance(refreshing.history, HistoryPending) and refreshing.history.connection is peer:
                fail_history(error)
            if peer is sockets.owner or not sockets.streams:
                transport_failed(error)
                delay = interval if sockets.streams or estimate_pending else 0.0
                reconnect_at = now + delay

        message = sockets.next_reply()
        if message is not None and message[0] is sockets.owner and isinstance(message[1], wire.Wait):
            wait = message[1]
            reconnect.reached(wait)
            last_valid = last_notice = now
            if refreshing is None or sockets.streams[message[0]].history_lookup is None:
                reconnect_at = math.inf
            cause = wait.reason.cause if isinstance(wait.reason, records.HeadAdmissionStatus) else wait.reason
            if not estimate_pending or not isinstance(cause, (records.EstimateExpired, records.ReadingUnavailable)):
                retry.unreachable_deadline = (now + configuration.unreachable_limit_seconds
                    if isinstance(configuration, wire.AcquisitionTiming) else math.inf)

        expired = deadline_limit()
        if expired is AcquisitionDeadline.LAUNCH or retry.launch_without_wait and had_wait:
            return request, AcquisitionDeadline.LAUNCH
        if now >= retry.next_leave:
            if retry.left_queue():
                return request, LeftQueue()
            continue
        if expired is not None:
            return request, expired
        history = refreshing.history if refreshing is not None else None

        if (isinstance(history, HistoryPending) and now >= history.deadline
                and (message is None or message[0] is not history.connection or isinstance(message[1], wire.Wait))):
            fail_history(CheckerFailure("no build history generation answer"))
            _begin_unreachable(retry, configuration)
            reconnect.disconnected()
            if len(sockets.streams) == 1:
                reconnect_at = min(reconnect_at, now)
            continue

        notice_at = max(last_valid, last_notice) + silence if sockets.streams else math.inf
        if now >= notice_at:
            if retry.launch_without_wait:
                return request, AcquisitionDeadline.LAUNCH
            _silence_notice(last_valid)
            last_notice = now
            _begin_unreachable(retry, configuration)
            reconnect.disconnected()
            if len(sockets.streams) == 1 and reconnect_at == math.inf:
                reconnect_at = now

        if reconnect_at <= now:
            if len(sockets.streams) < 2:
                if (expired := deadline_limit()) is not None:
                    return request, expired
                if isinstance(attempt, AdmissionHoldCheck) and not attempt.admits():
                    reconnect_at = CLOCK.monotonic() + interval
                else:
                    try:
                        if (expired := sockets.open(request, deadline_limit)) is not None:
                            return request, expired
                    except OSError as error:
                        if retry.launch_without_wait:
                            return request, AcquisitionDeadline.LAUNCH
                        transport_failed(error)
                        reconnect_at = now + interval
                    else:
                        reconnect_at = math.inf
                        if len(sockets.streams) == 1:
                            last_notice = now
                            answered_expiry = -math.inf
                        if isinstance(history, HistoryPending):
                            fail_history(CheckerFailure("build history connection replaced"))
            else:
                reconnect_at = math.inf
            # Give the selector a write opportunity after the connection is registered.
            sockets.poll(request, 0, deadline_limit)
            continue

        deadline = min(notice_at, retry.deadline, retry.next_leave, retry.unreachable_deadline,
                       reconnect_at, checker.next_attempt, checker.deadline,
                       history.deadline if isinstance(history, HistoryPending) else math.inf)
        if message is not None:
            try:
                peer, reply = message
                stream = sockets.streams[peer]
                _ = stream.replies.popleft()
                if isinstance(reply, wire.Wait):
                    wait_cause = reply.reason.cause if isinstance(reply.reason, records.HeadAdmissionStatus) else reply.reason
                    if peer is sockets.owner and isinstance(wait_cause, records.ReadingUnavailable):
                        raise CheckerFailure(wait_cause.reason)
                    had_wait = True
                    if retry.launch_without_wait:
                        continue
                    records.warn_configuration_diagnostics(reply.snapshot.diagnostics)
                    if peer is sockets.owner:
                        if isinstance(wait_cause, records.EstimateExpired):
                            if wait_cause.estimated_at <= answered_expiry or refreshing is not None:
                                continue
                            estimate_pending = True
                            refreshing = EstimateRefresh(wait_cause.estimated_at)
                        else:
                            estimate_pending = False
                            if refreshing is None:
                                checker.clear()
                    waiting_callback = (peer, reply)
                elif (isinstance(history, HistoryPending) and history.connection is peer
                      and stream.history_lookup == history.lookup_id):
                    if isinstance(reply, wire.BuildHistoryReply) and reply.generation >= 0:
                        stream.history_lookup = None
                        assert refreshing is not None
                        refreshing.history = HistoryDone(records.BuildHistoryGeneration(reply.generation))
                    else:
                        fail_history(reply if isinstance(reply, CheckerFailure)
                                     else CheckerFailure("invalid build history generation answer"))
                        reconnect_at = min(reconnect_at, now + interval)
                    continue
                elif isinstance(reply, (wire.BuildHistoryReply, CheckerFailure)) and stream.history_lookup is not None:
                    continue
                elif isinstance(history, HistoryPending) and history.connection is not peer:
                    continue
                else:
                    raise reply if isinstance(reply, CheckerFailure) else CheckerFailure("invalid admission answer")
            except CheckerFailure as error:
                if (expired := check_failed(error)) is not None:
                    return request, expired
                continue

        if (expired := deadline_limit()) is not None:
            return request, expired
        if checker.next_attempt <= now:
            checker_due = True
            checker.report(interval)
            if (expired := deadline_limit()) is not None:
                return request, expired
        elif checker.deadline < math.inf:
            if message is None:
                sockets.poll(request, max(0.0, deadline - CLOCK.monotonic()), deadline_limit)
            continue

        if waiting_callback is not None:
            callback_peer, waiting = waiting_callback
            on_wait(waiting)
            if (expired := deadline_limit()) is not None:
                return request, expired
            waiting_callback = None
            if callback_peer is sockets.owner and refreshing is None:
                checker.clear()
            continue

        peer = sockets.owner
        stream = sockets.streams.get(peer) if peer is not None else None
        # Wait confirms that the service read the preceding admission request.
        if (refreshing is not None and refreshed_write is None and not isinstance(history, HistoryPending)
                and peer is not None and stream is not None and stream.error is None
                and stream.received_wait and not stream.outgoing
                and (stream.history_lookup is None or isinstance(history, HistoryDone))):
            try:
                updated = _refresh_request(request, refresh, refreshing)
            except _HistoryNeeded:
                sockets.drain(request)
                if sockets.terminal() is None and stream.error is None:
                    if (expired := sockets.send(peer, history_request, deadline_limit)) is not None:
                        return request, expired
                    lookup_id += 1
                    refreshing.history = HistoryPending(lookup_id, peer, CLOCK.monotonic() + interval)
                    stream.history_lookup = lookup_id
            except (_QueueLeft, _QueueUnreachable):
                raise
            except Exception as error:
                if (expired := check_failed(error)) is not None:
                    return request, expired
            else:
                sockets.drain(request)
                if sockets.terminal() is None and stream.error is None:
                    if (expired := sockets.send(peer, updated, deadline_limit)) is not None:
                        return request, expired
                    refreshed_write = (peer, updated)
                    sockets.poll(request, 0, deadline_limit)
            continue

        if message is not None:
            continue
        sockets.poll(request, max(0.0, deadline - CLOCK.monotonic()), deadline_limit)


def _unreachable_duration(seconds: float) -> str:
    return f"{seconds / 60:g} min" if seconds % 60 == 0 else f"{seconds:g} s"


def admit(request: wire.AdmitRequest, on_wait: Callable[[wire.Wait], None],
          refresh: Callable[[], wire.AdmitRequest], *,
          _attempt: Callable[[], None] = _no_attempt_hook) -> wire.Admitted | wire.Refused | LeftQueue:
    return cast(wire.Admitted | wire.Refused | LeftQueue, _call(request, on_wait, refresh, attempt=_attempt))


def launch(request: wire.LaunchRequest, on_wait: Callable[[wire.Wait], None],
           refresh: Callable[[], wire.LaunchRequest]) -> wire.LaunchAdmitted | wire.Refused | LeftQueue:
    return cast(wire.LaunchAdmitted | wire.Refused | LeftQueue, _call(request, on_wait, refresh))


def _first_request_wait[Request, Reply](launching: bool, refresh: Callable[[], Request],
                                        wait: Callable[[Request], Reply]) -> Reply | wire.Refused | LeftQueue:
    """Build the first request inside the retry schedule that its refreshes use."""
    limit = _whole_seconds("BUILDLOG_MEM_WAIT_LIMIT_S", 900)
    token = _ACQUISITION_RETRY.set(AcquisitionRetry(CLOCK.monotonic() + limit if launching and limit > 0 else float("inf"),
                                                    launch_without_wait=launching and limit == 0))
    try:
        try:
            request = refresh()
        except (_QueueLeft, _QueueUnreachable):
            raise
        except Exception as error:
            return _refused(str(error) or "checker returned an error", checker=True)
        return wait(request)
    except _QueueLeft:
        return LeftQueue()
    except _QueueUnreachable as error:
        return _refused(str(error))
    finally:
        _ACQUISITION_RETRY.reset(token)


def wait_for_admission(refresh: Callable[[], wire.AdmitRequest], on_wait: Callable[[wire.Wait], None], *,
                       attempt: Callable[[], None] = _no_attempt_hook) -> wire.Admitted | wire.Refused | LeftQueue:
    """Wait for a step or program reservation. Only queue_place calls this."""
    return _first_request_wait(False, refresh, lambda request: admit(request, on_wait, refresh, _attempt=attempt))


def wait_for_launch(refresh: Callable[[], wire.LaunchRequest],
                    on_wait: Callable[[wire.Wait], None]) -> wire.LaunchAdmitted | wire.Refused | LeftQueue:
    """Wait for a launch start, which has no reservation. Only queue_place calls this."""
    return _first_request_wait(True, refresh, lambda request: launch(request, on_wait, refresh))


def _silence_notice(last_valid: float) -> None:
    from scripts.lib.user_zone import user_zone
    since = datetime.fromtimestamp(CLOCK.now() - (CLOCK.monotonic() - last_valid), user_zone()).strftime("%H:%M")
    print(f"admission service not answering since {since}", file=sys.stderr)


def _mutation(request: wire.ReleaseRequest | wire.WidthRequest | wire.PrioritySetRequest | wire.PriorityClearRequest | wire.TokenWaitRequest,
              configuration: records.MemoryConfig | wire.CleanupTiming | ReadConfigurationAtUse = READ_CONFIGURATION_AT_USE) -> wire.Ok | wire.Refused:
    return cast(wire.Ok | wire.Refused, _call(request, lambda waiting: None, configuration=configuration))


def request_id() -> str:
    """Keep replay identities inside the connecting account's namespace."""
    return f"uid:{os.getuid()}:{uuid.uuid4().hex}"


def repository_slot(request: wire.RepositorySlotRequest) -> wire.RepositorySlotReady | wire.RepositorySlotExpired | wire.Refused:
    """Wait on the queue connection without polling the repository pool."""
    reply = _call(request, lambda waiting: None, configuration=wire.PROTOCOL_ACQUISITION_TIMING)
    if isinstance(reply, (wire.RepositorySlotReady, wire.RepositorySlotExpired, wire.Refused)):
        return reply
    return wire.Refused("left the repository slot queue")


def _take_verify_run_slot(pid: int, cwd: str, placement_episode: str) -> int:
    """Wait on the admission connection for the Verify process's slot."""
    from scripts.build_queue import step_estimator, admission_records as records

    identity = step_identity("", cwd)
    start = records.process_start_time(pid)
    if start is None:
        print("verify.sh: cannot identify the Verify process", file=sys.stderr)
        return 2
    repository, _ = step_estimator.git_identity(["cargo", "check"], cwd)
    name = repository.name if isinstance(repository, records.RepositoryIdentity) else Path(cwd).name
    request = wire.VerifyRunSlotRequest(wire.Owner(pid, start), name, identity.session_id, identity.unit,
        worktree=cwd, verify=verify_call(), placement_episode=placement_episode)
    reply = _call(request, lambda waiting: None)
    return 0 if isinstance(reply, wire.Ok) else 2


def service_admit(request: wire.ServiceEstimateRequest, on_wait: Callable[[wire.Wait], None],
                  timing: wire.AcquisitionTiming = wire.PROTOCOL_ACQUISITION_TIMING
                  ) -> wire.Admitted | wire.Refused | LeftQueue:
    reply = _call(request, on_wait, configuration=timing)
    if isinstance(reply, (wire.Admitted, wire.Refused, LeftQueue)):
        return reply
    return wire.Refused("invalid service estimate answer")


def release(reservation: str, status: int, configuration: records.MemoryConfig | wire.CleanupTiming) -> wire.Ok | wire.Refused:
    return _mutation(wire.ReleaseRequest(request_id(), reservation, status), configuration)


def width(reservation: str, width: int) -> wire.Ok | wire.Refused:
    return _mutation(wire.WidthRequest(request_id(), reservation, width))


def priority_set(unit: str, why: str, actor: str) -> wire.Ok | wire.Refused:
    return _mutation(wire.PrioritySetRequest(uuid.uuid4().hex, unit, why, actor))


def priority_clear(actor: str, unit: str = "") -> wire.Ok | wire.Refused:
    return _mutation(wire.PriorityClearRequest(uuid.uuid4().hex, actor, unit))


def token_wait(reservation: str, configuration: records.MemoryConfig | wire.CleanupTiming) -> wire.Ok | wire.Refused:
    return _mutation(wire.TokenWaitRequest(request_id(), reservation), configuration)


def wait_reasons(result: records.AdmissionWaitSnapshot) -> tuple[str, ...]:
    """Describe why admission holds, without treating changing samples as a new reason."""
    from scripts.build_queue import admission_records as records
    cause = result.head_reason.cause if isinstance(result.head_reason, records.HeadAdmissionStatus) else result.head_reason
    if isinstance(cause, records.MemoryPressure) and cause.condition == "some":
        return ("some memory pressure",)
    if isinstance(cause, records.BehindHead):
        return (f"behind {snapshot_codec.select_queue_name(cause.head_name)}",)
    if isinstance(cause, records.ReadyForAdmission):
        return ("admission refresh",)
    return (records.reason_phrase(cause, ()),)


def blocking_waiter_text(place: str, label: str, need_gib: float) -> str:
    """Show the blocking step's position and memory need."""
    if place == "behind #1":
        label = f"head {label} needs {need_gib:.1f} GiB"
    return " · ".join(part for part in (place, label) if part)


@dataclass(frozen=True)
class UnseenWaitHolder:
    pass


@dataclass(frozen=True)
class QueuedPlacementEpisode:
    episode: str


@dataclass(frozen=True)
class PlacementWaitNotice:
    started: float
    last_reasons: tuple[str, ...]
    last_position: int
    last_reply: wire.Wait | wire.PlacementResourceWait
    elapsed_memory_seconds: float = 0.0
    provider_diagnostics: tuple[placement.BusyProviderDiagnostic, ...] = ()


def first_hana_image_notice() -> bool:
    """Share the notice with other clients that use the same verify run folder."""
    hold_wait_file = os.environ.get("BUILDLOG_HOLD_WAIT_FILE", "")
    if hold_wait_file:
        try:
            with Path(hold_wait_file).with_name("hana-image-stale").open("x"):
                pass
        except FileExistsError:
            return False
        except OSError:
            pass
    return True


@dataclass
class WaitOutput:
    started: float = field(default_factory=lambda: CLOCK.monotonic())
    deadline_reached: bool = False
    held: bool = False
    last_reasons: tuple[str, ...] = ()
    last_position: int = 0
    last_holder: records.WaitHolder | UnseenWaitHolder = field(default_factory=UnseenWaitHolder)
    on_event: Callable[[str], None] = field(default=lambda _event: None)
    last_resource: wire.PlacementResourceWait | wire.PlacementEvidenceWait = field(default_factory=wire.PlacementEvidenceWait)
    elapsed_memory_seconds: float = 0.0
    last_provider_diagnostics: tuple[placement.BusyProviderDiagnostic, ...] = ()
    announced_retries: set[str] = field(default_factory=set)
    announced_hana_image_stale: bool = False
    status_open: bool = False
    local_reason: str = field(default_factory=lambda: os.environ.get("BUILD_QUEUE_LOCAL_REASON", ""))

    def retry_notices(self, notices: tuple[placement.ProviderRetryNotice, ...], *, placement_events: bool) -> None:
        for retry in notices:
            if retry.target not in self.announced_retries:
                target = "Mac" if retry.target == "mac" else retry.target
                text = (f"verify.sh: {target} attempt ended ({retry.reason}); "
                        f"waiting {retry.delay_seconds:g} s before each retry while natedev waits.")
                if placement_events:
                    self.on_event("retry " + text)
                else:
                    self.end_status()
                    print(text, file=sys.stderr, flush=True)
                self.announced_retries.add(retry.target)

    def provider_diagnostics(self, diagnostics: tuple[placement.BusyProviderDiagnostic, ...]) -> None:
        from scripts.lib.user_zone import user_zone

        for diagnostic in diagnostics:
            if diagnostic not in self.last_provider_diagnostics:
                since = datetime.fromisoformat(diagnostic.since).astimezone(user_zone()).strftime("%H:%M")
                self.end_status()
                print(f"busy: {diagnostic.what} since {since}", file=sys.stderr)
                self.on_event("provider " + json.dumps({"target": diagnostic.target, "what": diagnostic.what}, separators=(",", ":")))
        self.last_provider_diagnostics = diagnostics

    def placement_notices(self, reply: wire.Assignment | wire.LocalPlacementGranted | wire.PlacementWait | wire.DispatchStatus,
                          *, placement_events: bool) -> None:
        from scripts.build_queue import placement

        if isinstance(reply, (wire.PlacementWait, wire.Assignment, wire.LocalPlacementGranted)):
            self.provider_diagnostics(reply.provider_diagnostics)
            if reply.mac_disk_exclusions:
                self.on_event("disk " + ",".join(reply.mac_disk_exclusions))
        if isinstance(reply, wire.PlacementWait):
            self.retry_notices(reply.retry_notices, placement_events=placement_events)
        elif (isinstance(reply, (wire.Assignment, wire.LocalPlacementGranted)) and reply.hana_image_stale
              and not self.announced_hana_image_stale):
            self.announced_hana_image_stale = True
            if first_hana_image_notice():
                self.end_status()
                print(placement.HANA_IMAGE_STALE, file=sys.stderr, flush=True)

    def resource_wait(self, reply: wire.PlacementResourceWait) -> None:
        from scripts.build_queue import placement

        previous = self.last_resource
        changed = not isinstance(previous, wire.PlacementResourceWait) or (
            previous.resource, previous.reason) != (reply.resource, reply.reason)
        if self.held and changed:
            if not isinstance(previous, wire.PlacementResourceWait):
                self.elapsed_memory_seconds += CLOCK.monotonic() - self.started
            self.on_event("done")
            self.held = False
        if not self.held or previous != reply:
            holder = ({"kind": "session", "session_id": reply.holder.session_id, "session_name": ""}
                      if isinstance(reply.holder, placement.ResourceHolderSession) else
                      {"kind": "unit", "unit": reply.holder.unit}
                      if isinstance(reply.holder, placement.ResourceHolderUnit) else {"kind": "none"})
            self.on_event("resource " + json.dumps({"resource": reply.resource, "reason": reply.reason,
                "holder": holder}, separators=(",", ":")))
            if self.local_reason:
                self.status(f"waiting for {reply.resource}: {reply.reason}; runs on natedev: {self.local_reason}")
        self.held = True
        self.last_resource = reply

    def status(self, text: str) -> None:
        """Print one wait status line; on a terminal, write it over the last status line."""
        if sys.stderr.isatty():
            print("\r\x1b[K" + text, end="", file=sys.stderr, flush=True)
            self.status_open = True
        else:
            print(text, file=sys.stderr, flush=True)

    def end_status(self) -> None:
        """End a status line that a terminal holds open, before other output."""
        if self.status_open:
            print(file=sys.stderr, flush=True)
            self.status_open = False

    def __call__(self, reply: wire.Wait) -> None:
        from scripts.build_queue import admission_records as records
        from scripts.lib.user_zone import user_zone
        if isinstance(self.last_resource, wire.PlacementResourceWait):
            self.on_event("done")
            self.held = False
            self.last_resource = wire.PlacementEvidenceWait()
            self.started = CLOCK.monotonic()
        first = not self.held
        if isinstance(reply.reason, wire.CloudSlotWait):
            reasons = (reply.text,)
            if first:
                self.held = True
                self.on_event("first")
                self.on_event('holder {"kind":"none"}')
            if first or reasons != self.last_reasons:
                self.status(reply.text)
                self.last_reasons = reasons
            return
        snapshot = reply.snapshot
        cause = snapshot.head_reason
        if isinstance(cause, records.HeadAdmissionStatus):
            cause = cause.cause
        order = snapshot.decision.order_hold
        if isinstance(cause, records.BehindHead) and isinstance(order, records.HeldBehindHead):
            blocker = order.waiter
            label = snapshot_codec.wait_line_name(blocker.get("unit", ""), blocker.get("session_name", ""),
                blocker.get("folder", ""), blocker.get("step", ""), fallback_name=cause.head_name)
            reasons = (blocking_waiter_text(reply.text, label, blocker["need"] / records.GIB),)
        else:
            reasons = wait_reasons(snapshot)
        behind = isinstance(cause, records.BehindHead)
        if first:
            self.held = True
            self.on_event("first")
        if first or reply.snapshot.holder != self.last_holder:
            holder = reply.snapshot.holder
            payload = ({"kind": "session", "session_id": holder.session_id, "session_name": holder.session_name}
                       if isinstance(holder, records.SessionHolder) else
                       {"kind": "unit", "unit": holder.unit} if isinstance(holder, records.UnitHolder) else {"kind": "none"})
            self.on_event("holder " + json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
            self.last_holder = reply.snapshot.holder
        if not first and reasons == self.last_reasons and reply.position == self.last_position:
            return
        self.last_reasons, self.last_position = reasons, reply.position
        prefix = ("waiting for memory since " + datetime.fromtimestamp(CLOCK.now(), user_zone()).strftime("%H:%M %Z")
                  if first else f"still waiting for memory ({int(CLOCK.monotonic() - self.started) // 60} min)")
        place = ("this build is the queue head" if reply.position == 1 else
                 f"place {reply.position} in line" if reply.position > 1 else "")
        subject = " ".join(name for value in (snapshot.repo, snapshot.step)
                           if (name := snapshot_codec.public_queue_text(value))) or "build"
        reason = ("reason: " + reasons[0] if behind else records.head_wait_text(snapshot.head_reason, user_zone())
            if isinstance(snapshot.head_reason, records.HeadAdmissionStatus) else
            records.pressure_text(snapshot.decision.pressure)
            if isinstance(snapshot.decision.pressure, records.MemoryPressure) else "reason: " + "; ".join(reasons))
        head = (f"head {snapshot_codec.select_queue_name(snapshot.head_label)} needs {snapshot.head_need / records.GIB:.1f} GiB"
                if not behind and snapshot.head_need and reply.position != 1 else "")
        parts = (place, f"{subject} needs {snapshot.peak.bytes / records.GIB:.1f} GiB",
                 f"{snapshot.available / records.GIB:.1f} GiB free", reason, head,
                 f"runs on natedev: {self.local_reason}" if self.local_reason else "",
                 "/build:page shows the detail" if first else "")
        self.status(f"{prefix}: " + "; ".join(part for part in parts if part))

    def elapsed_seconds(self) -> int:
        if isinstance(self.last_resource, wire.PlacementResourceWait):
            return int(self.elapsed_memory_seconds)
        return int(self.elapsed_memory_seconds + CLOCK.monotonic() - self.started) if self.held or self.deadline_reached else 0

    def finished(self, *, left: bool = False, placement_events: bool = False) -> None:
        if not self.held:
            return
        if isinstance(self.last_resource, wire.PlacementResourceWait):
            if not (left and self.last_resource.resource == "natedev" and self.last_resource.reason == "blocked"):
                return
            text = "verify.sh: another machine is free; leaving natedev's hold wait to run there."
        else:
            seconds = self.elapsed_seconds()
            text = (f"another machine is free after {seconds // 60} min {seconds % 60} s: leaving the memory wait" if left else
                    f"memory free after {seconds // 60} min {seconds % 60} s: starting")
        self.end_status()
        if left and placement_events:
            self.on_event("left " + text)
        else:
            print(text, file=sys.stderr, flush=True)


def parse_run_identity(value: str) -> wire.RunIdentity | wire.NoRun:
    """Parse the run identity at the client boundary."""
    if sys.platform == "darwin":
        return wire.NO_RUN
    pid, separator, start = value.partition(":")
    if separator and pid.isascii() and pid.isdecimal() and int(pid) > 0 and start.isascii() and start.isdecimal():
        return wire.RunIdentity(int(pid), start)
    return wire.NO_RUN


@dataclass(frozen=True)
class InferProfileAtUse:
    pass


INFER_PROFILE_AT_USE = InferProfileAtUse()


def step_identity(step: str, worktree: str) -> wire.Identity:
    from scripts.build_queue import admission_records as records

    identity = records.waiter_identity(step, worktree)
    return wire.Identity(identity["session_id"], identity["session_name"], identity["step"],
                         identity["folder"], identity.get("unit", ""))


def verify_call() -> records.VerifyStep:
    """Read the verify call facts from the shell that starts the step."""
    from scripts.build_queue import admission_records as records

    name = os.environ.get("BUILDLOG_VERIFY_VERB")
    call = os.environ.get("BUILDLOG_VERIFY_CALL", "")
    if name not in records.VERIFY_VERBS or not call:
        return records.NotVerifyCall()
    try:
        position = int(os.environ.get("BUILDLOG_VERIFY_POSITION", ""))
        total = int(os.environ.get("BUILDLOG_VERIFY_TOTAL", ""))
    except ValueError:
        return records.NotVerifyCall()
    if 1 <= position <= total:
        return records.VerifyCall(name, call, position, total)
    return records.NotVerifyCall()


def step_request(argv: list[str], cwd: str, owner: wire.Owner, since: float, sidecar: str,
                 episode: str, profile: records.InferFromArgv | wire.Profile | InferProfileAtUse = INFER_PROFILE_AT_USE,
                 *, run: wire.RunIdentity | wire.NoRun = wire.NO_RUN
                 ) -> wire.AdmitRequest:
    from scripts.build_queue import step_estimator, admission_records as records
    from scripts.buildlog import store

    if isinstance(run, wire.NoRun):
        run = parse_run_identity(os.environ.get("BUILDLOG_RUN", ""))
    if isinstance(profile, InferProfileAtUse):
        profile = records.INFER_FROM_ARGV
    config = records.read_memory_config()
    estimate = step_estimator.step_estimate(argv, cwd, profile, lambda: datetime.fromtimestamp(CLOCK.now(), UTC),
                                  store.root(), config.policy, first_peak_bytes=config.program_first_peaks["cargo"],
                                  build_history_generation=lambda: build_history_generation(config))
    width_value = records.test_threads(argv)
    width_value = width_value or (int(os.environ["NEXTEST_TEST_THREADS"]) if os.environ.get("NEXTEST_TEST_THREADS", "").isdecimal() else None)
    width_value_typed: records.TestWidth = (records.RequestedTestWidth(width_value) if width_value else
        records.UnspecifiedTestWidth() if estimate.profile.step == "nextest" else records.NoTestWidth())
    call = os.environ.get("BUILDLOG_CALL_ID", "")
    identity = replace(step_identity(estimate.profile.step, estimate.profile.worktree), verify=verify_call())
    if isinstance(estimate.profile, records.ProgramProfile):
        identity = replace(identity, folder=estimate.profile.worktree)
    return wire.AdmitRequest(episode, owner, tuple(argv), cwd, since, sidecar,
        identity,
        records.CallRecord(call) if call else records.NoCallRecord(), width_value_typed, estimate.profile,
        isinstance(estimate.cache, records.FreshTest), float(os.environ.get("BUILDLOG_MEM_TOKEN_BACK_AT", "0")), run,
        route=records.direct_cargo_route())


def write_shell_grant(path: Path, grant: wire.Admitted | wire.LaunchAdmitted, elapsed: int) -> None:
    """Write assignments in the client process; the shell parses no protocol data."""
    values = {"BUILDLOG_MEM_OUTCOME": "Granted", "BUILDLOG_MEM_WAIT_S": str(elapsed)}
    if isinstance(grant, wire.Admitted):
        values.update(BUILDLOG_MEM_RESERVATION=grant.reservation, BUILDLOG_MEM_MAX_PATH=grant.max_path,
            BUILDLOG_MEM_WAVE_PATH=grant.wave_path, BUILDLOG_MEM_SAMPLING_CONTEXT=wire.encode_record(grant.sampling_context),
            BUILDLOG_MEM_CLEANUP_TIMING=wire.encode_record(grant.cleanup_timing))
    _ = path.write_text("".join(f"{key}={shlex.quote(value)}\n" for key, value in values.items()))


def down_alert() -> int:
    from scripts.accounts import outage_alerts
    from scripts.build_queue import admission_records as records
    from scripts.message import send_budget, send_result
    from scripts.message.session_send import session_send_argv
    configuration = records.read_memory_config()
    started = CLOCK.monotonic()
    while True:
        try:
            _ = view()
            return 0
        except wire.AdmissionUnavailable:
            if CLOCK.monotonic() - started >= configuration.silence_seconds:
                break
            CLOCK.sleep(configuration.reconnect_seconds)
    settings = conf.read("lint")
    claimed = outage_alerts.claim("build-admission")
    if not isinstance(claimed, outage_alerts.AlertClaim):
        return 0

    def send(command: list[str], message: str = "") -> send_result.Outcome:
        try:
            result = subprocess.run(command, input=message, text=True, capture_output=True,
                                    timeout=send_budget.seconds(settings.number("sweep_sender_timeout_seconds")))
            header = send_result.parse_outcome(result.stdout)
            if isinstance(header, send_result.OutcomeHeader):
                print(f"{header.outcome.upper()}: {header.detail}", flush=True)
            outcome = send_result.send_outcome(result.returncode, result.stdout)
            if isinstance(outcome, send_result.ConfirmedSendOutcome):
                return outcome.header.outcome
            print(f"build admission: outage alert failed: {result.stderr.strip()}", file=sys.stderr)
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            print(f"build admission: outage alert failed: {error}", file=sys.stderr)
        return "failed"

    retained = False
    try:
        restart = "systemctl reset-failed build-admission.socket build-admission.service && systemctl start build-admission.socket build-admission.service"
        message = (f"The build queue service has not answered for {configuration.silence_seconds:g} seconds. Builds wait.\n"
                   "The service restarts by itself. Read journalctl -u build-admission.service.\n"
                   f"If the user must act, push the phone with this command: {restart}")
        session_outcome = send(session_send_argv(BUILDS_SESSION, sender="build-admission", lint_settings=settings), message)
        release_outcomes = ("failed", "refused", "retry")
        retained = session_outcome not in release_outcomes
        if session_outcome not in ("sent", "kept"):
            from scripts.notify import push_texts
            command = push_texts.user_send_argv(push_texts.render("alert.blocked",
                sender="build-admission", title="Build queue service is down",
                action=f"On natedev, run: {restart}",
                body="The builds session did not take the message. Builds wait until the service answers."))
            scripts = Path(__file__).resolve().parents[1]
            command.extend(["--clears-when", shlex.join([str(scripts / "lib/py"), str(Path(__file__).resolve()), "view"])])
            phone_outcome = send(command)
            retained = retained or phone_outcome not in release_outcomes
        return 0 if retained else 1
    finally:
        if retained:
            outage_alerts.delivered(claimed)
        else:
            outage_alerts.release(claimed)


def saved_cleanup_timing() -> wire.CleanupTiming:
    raw = os.environ.get("BUILDLOG_MEM_CLEANUP_TIMING")
    if raw:
        try:
            timing = wire.parse_record(raw, wire.CleanupTiming)
            if all(math.isfinite(value) and value > 0 for value in (timing.silence_seconds, timing.reconnect_seconds)):
                return timing
        except (ValueError, TypeError):
            pass
    return wire.PROTOCOL_CLEANUP_TIMING


class ClientArguments(argparse.Namespace):
    command: str = ""
    session_id: str = ""
    reservation: str = ""
    value: int = 0
    why: str = ""
    unit: str = ""
    pid: int = 0
    since: float = 0.0
    sidecar: str = ""
    shell_state: Path | None = None
    wait_events: bool = False
    argv: list[str] = []
    episode: str = ""
    attempt: str = ""
    repo_root: str = ""
    cargo_folder: Path | None = None
    package: str = ""
    kind: str = ""
    source_key: str = ""
    target: str = ""
    run: str = ""
    parent_episode: str = ""
    call_started: float = 0.0
    assignment_file: Path = Path()
    token: str = "none"
    writes_tree: bool = False
    local_only: bool = False
    local_reason: str = ""
    mac_only: bool = False
    pinned_refusals: bool = False
    once: bool = False
    reason: str = ""
    status: int = 0
    request_file: Path | None = None


def constrained_step_route(route: target_config.StepRoute,
                           constraints: placement.PlacementConstraints) -> target_config.StepRoute:
    """Show the fixed local reason when every remote candidate is excluded."""
    if not isinstance(route, target_config.AnyEligibleTarget):
        return route
    remote = tuple(candidate for candidate in constraints.candidates if candidate.destination.provider != "local")
    if not remote or any(not isinstance(candidate.eligibility, placement.FixedExclusion) for candidate in remote):
        return route
    reasons = dict.fromkeys(candidate.eligibility.reason for candidate in remote
                            if isinstance(candidate.eligibility, placement.FixedExclusion))
    return target_config.LocalRequired("; ".join(reasons))


def _print_placement_refusal(reason: str) -> int:
    print("refused\t" + " ".join(reason.splitlines()))
    return 2


def _place_step(args: ClientArguments, output: WaitOutput) -> int:
    output.local_reason = args.local_reason or output.local_reason
    if args.command == "cancel-placement":
        from scripts.build_queue import placement, admission_records as records
        from scripts.buildlog import store

        start = records.process_start_time(args.pid)
        if start is None:
            raise CheckerFailure("owner no longer exists")
        owner = placement.SubmittingOwner(store.host_name(), args.pid, start)
        _ = cancel_placement(wire.CancelPlacement(uuid.uuid4().hex, args.episode, owner, args.reason))
        return 0
    placement_wait_state = args.assignment_file.with_name(args.assignment_file.name + ".wait")
    queued_episode_state = args.assignment_file.with_name(args.assignment_file.name + ".queued")
    already_queued = False
    keep_wait = False
    pinned_refusal_reported = False
    try:
        if args.command == "place-step" and placement_wait_state.exists():
            try:
                saved = wire.parse_record(placement_wait_state.read_text(), PlacementWaitNotice)
            except (ValueError, TypeError):
                placement_wait_state.unlink(missing_ok=True)
            else:
                output.started, output.last_reasons = saved.started, saved.last_reasons
                output.last_position = saved.last_position
                output.elapsed_memory_seconds = saved.elapsed_memory_seconds
                output.last_provider_diagnostics = saved.provider_diagnostics
                if isinstance(saved.last_reply, wire.Wait):
                    output.last_holder = saved.last_reply.snapshot.holder
                else:
                    output.last_resource = saved.last_reply
                output.held = True
        from scripts.build_queue import placement, admission_records as records
        from scripts.buildlog import store

        start = records.process_start_time(args.pid)
        if start is None:
            raise CheckerFailure("owner no longer exists")
        owner = placement.SubmittingOwner(store.host_name(), args.pid, start)
        from scripts.build_hold.owning_session import NoOwningSession, OwningSession

        step_argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
        episode = args.episode or f"{args.pid}-{start}-{args.source_key}"
        if queued_episode_state.exists():
            try:
                queued = wire.parse_record(queued_episode_state.read_text(), QueuedPlacementEpisode)
            except (ValueError, TypeError):
                queued_episode_state.unlink(missing_ok=True)
            else:
                already_queued = queued.episode == episode
        if args.assignment_file.exists():
            try:
                assigned = read_assignment(args.assignment_file)
            except (FileNotFoundError, ValueError, wire.AdmissionUnavailable):
                pass
            else:
                already_queued = already_queued or assigned.assignment.episode == episode
        def refresh_local() -> wire.AdmitRequest:
            return step_request(step_argv, os.getcwd(), wire.Owner(args.pid, start), args.since,
                                args.sidecar, episode, run=parse_run_identity(args.run))

        cargo_folder: placement.CargoFolder | placement.NoCargoFolder = placement.NoCargoFolder()
        if args.cargo_folder is not None:
            if not args.cargo_folder.is_absolute():
                raise CheckerFailure("the Cargo folder must be an absolute path")
            cargo_folder = placement.CargoFolder(args.cargo_folder.resolve())

        local = refresh_local()
        session = OwningSession(args.session_id) if args.session_id else NoOwningSession()
        # A Mac-only step is pinned to the Mac, so the routing rule lets a build leave natedev.
        selection = (step_selection.Pinned(args.target) if args.target else
                     step_selection.Pinned("mac") if args.mac_only else step_selection.ROUTED)
        routing = (target_config.LocalRequired(args.local_reason) if args.local_reason else
                   target_config.LOCAL_CALLER if args.local_only else target_config.ANY_ELIGIBLE_TARGET)
        kind = args.kind or local.profile.step
        if kind == "nextest":
            kind = "test"
        elif kind in ("clippy", "mend-check"):
            kind = "lint"
        builder = cast(PlacementConstraintBuilder, getattr(
            import_module("scripts.build_targets.build_targets"), "fixed_placement_constraints"))
        constraints = builder(Path(args.repo_root or os.getcwd()), args.package,
            cast(placement.CommandKind, kind), tuple(step_argv), session,
            execution_identity.SourceKeyKnown(args.source_key) if args.source_key else execution_identity.SourceKeyUnavailable("source key is unavailable"),
            routing, selection,
            writes_tree=args.writes_tree)
        constraints = replace(constraints, cargo_folder=cargo_folder)
        if args.mac_only and not args.writes_tree:
            constraints = replace(constraints, selection=placement.MacOnlyHost())
        prerequisite = (placement.TokenRequired(placement.TokenReadinessUnchecked())
                        if args.token in TOKEN_REQUIRED_CHOICES and args.once and not already_queued else
                        token_prerequisite(args.token) if args.token in TOKEN_REQUIRED_CHOICES else
                        placement.TokenHeld() if args.token == "held" else placement.TokenNotRequired())
        request = wire.PlacementRequest(episode, owner, args.since, local.profile, constraints,
            identity=local.identity, run=local.run,
            local=wire.LocalAdmissionContext(local.argv, local.cwd, local.sidecar, local.call,
                local.test_width, local.fresh_test, local.token_back_at), prerequisite=prerequisite,
            purpose=placement.SpeculativeCopy(args.parent_episode, args.call_started)
                if args.parent_episode else placement.NormalStep(),
            route=constrained_step_route(target_config.step_route(
                local=args.local_only, mac_only=args.mac_only, target=args.target,
                writes_tree=args.writes_tree, local_reason=args.local_reason), constraints))
        if args.request_file is not None:
            write_text_atomic(args.request_file, wire.encode_request(request), new_file_mode=0o600)
            return 0
        report_refusal = not args.pinned_refusals
        if already_queued:
            _ = report_prerequisite(wire.PrerequisiteReport(uuid.uuid4().hex, episode, owner, prerequisite),
                                    report_refusal=report_refusal)
        while True:
            answer = request_placement(request, report_refusal=report_refusal)
            output.placement_notices(answer, placement_events=args.wait_events)
            if isinstance(answer, wire.PlacementWait) and isinstance(answer.wait, wire.Wait):
                _ = report_prerequisite(wire.PrerequisiteReport(uuid.uuid4().hex, episode, owner, prerequisite),
                                        report_refusal=report_refusal)
                answer = request_placement(request, report_refusal=report_refusal)
                output.placement_notices(answer, placement_events=args.wait_events)
            if not isinstance(answer, wire.PlacementWait):
                break
            if _leave_queue():
                _ = cancel_placement(wire.CancelPlacement(uuid.uuid4().hex, episode, owner,
                    "the caller left before placement"))
                raise _QueueLeft()
            # The first pass only lets a free remote target take the step; it is no memory wait.
            token_check = (isinstance(prerequisite, placement.TokenRequired)
                           and isinstance(prerequisite.readiness, placement.TokenReadinessUnchecked))
            if isinstance(answer.wait, wire.Wait) and not token_check:
                output(answer.wait)
                notice = PlacementWaitNotice(output.started, output.last_reasons,
                    output.last_position, answer.wait, output.elapsed_memory_seconds, output.last_provider_diagnostics)
                write_text_atomic(placement_wait_state, wire.encode_record(notice), new_file_mode=0o600)
            elif isinstance(answer.wait, wire.PlacementResourceWait):
                output.resource_wait(answer.wait)
                notice = PlacementWaitNotice(output.started, output.last_reasons,
                    output.last_position, answer.wait, output.elapsed_memory_seconds, output.last_provider_diagnostics)
                write_text_atomic(placement_wait_state, wire.encode_record(notice), new_file_mode=0o600)
            if isinstance(answer.wait, wire.Wait):
                cause = answer.wait.reason
                if isinstance(cause, records.HeadAdmissionStatus):
                    cause = cause.cause
                if isinstance(cause, records.EstimateExpired):
                    local = cast(wire.AdmitRequest, _refreshed(local, refresh_local))
                    request = replace(request, profile=local.profile,
                        local=wire.LocalAdmissionContext(local.argv, local.cwd, local.sidecar, local.call,
                            local.test_width, local.fresh_test, local.token_back_at))
                    continue
            if args.once:
                if answer.eligibility:
                    write_text_atomic(queued_episode_state, wire.encode_record(QueuedPlacementEpisode(episode)),
                                      new_file_mode=0o600)
                keep_wait = True
                if not args.wait_events:
                    print(wire.encode_reply(answer), end="")
                return 78
            CLOCK.sleep(records.read_memory_config().waiting_pass_seconds)
        if isinstance(answer, wire.DispatchStatus):
            reason = answer.state.reason if isinstance(answer.state, placement.ClosedAssignment) else answer.reason
            if args.pinned_refusals:
                pinned_refusal_reported = True
                return _print_placement_refusal(reason)
            if args.mac_only:
                print("refused\t" + reason)
            else:
                print(wire.encode_reply(answer), end="")
            return 2
        write_assignment(args.assignment_file, answer)
        if isinstance(answer, wire.LocalPlacementGranted) and args.shell_state is not None:
            write_shell_grant(args.shell_state, answer.grant, output.elapsed_seconds())
        output.finished(left=isinstance(answer, wire.Assignment), placement_events=args.wait_events)
        if output.held:
            output.on_event("done")
            output.held = False
        destination = answer.assignment.destination
        print("\t".join((destination.provider, destination.target, destination.host, str(destination.slot),
                         answer.assignment.episode, answer.assignment.attempt)))
        return 0
    finally:
        if not keep_wait:
            if output.held:
                output.on_event("done")
                output.held = False
            if args.command == "place-step":
                try:
                    placement_wait_state.unlink(missing_ok=True)
                    queued_episode_state.unlink(missing_ok=True)
                except OSError as error:
                    if not pinned_refusal_reported:
                        raise
                    print(str(error), file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name in ("view", "explain", "down-alert", "priority-clear", "code-version", "check-nested"):
        _ = subcommands.add_parser(name)
    status_parser = subcommands.add_parser("placement-status")
    _ = status_parser.add_argument("--episode", required=True)
    _ = status_parser.add_argument("--attempt", required=True)
    run_parser = subcommands.add_parser("verify-run-slot")
    _ = run_parser.add_argument("--pid", type=int, required=True)
    _ = run_parser.add_argument("--episode", default="")
    placement_parser = subcommands.add_parser("place-step")
    for name in ("episode", "repo-root", "package", "kind", "source-key", "target", "session-id", "run", "sidecar", "parent-episode"):
        _ = placement_parser.add_argument("--" + name, default="")
    _ = placement_parser.add_argument("--cargo-folder", type=Path)
    _ = placement_parser.add_argument("--pid", type=int, default=os.getppid())
    _ = placement_parser.add_argument("--since", type=float, required=True)
    _ = placement_parser.add_argument("--call-started", type=float, default=0.0)
    _ = placement_parser.add_argument("--assignment-file", type=Path, required=True)
    _ = placement_parser.add_argument("--shell-state", type=Path)
    _ = placement_parser.add_argument("--request-file", type=Path)
    _ = placement_parser.add_argument("--token", choices=(*TOKEN_REQUIRED_CHOICES, "held", "none"), default="none")
    _ = placement_parser.add_argument("--writes-tree", action="store_true")
    _ = placement_parser.add_argument("--local-only", action="store_true")
    _ = placement_parser.add_argument("--local-reason", default=os.environ.get("BUILD_QUEUE_LOCAL_REASON", ""))
    _ = placement_parser.add_argument("--mac-only", action="store_true")
    _ = placement_parser.add_argument("--once", action="store_true")
    _ = placement_parser.add_argument("--wait-events", action="store_true")
    _ = placement_parser.add_argument("--pinned-refusals", action="store_true")
    _ = placement_parser.add_argument("argv", nargs=argparse.REMAINDER)
    cancel_parser = subcommands.add_parser("cancel-placement")
    _ = cancel_parser.add_argument("--episode", required=True)
    _ = cancel_parser.add_argument("--pid", type=int, default=os.getppid())
    _ = cancel_parser.add_argument("--reason", default="caller cancelled")
    finish_parser = subcommands.add_parser("finish-step")
    _ = finish_parser.add_argument("--assignment-file", type=Path, required=True)
    _ = finish_parser.add_argument("--status", type=int, default=1)
    _ = finish_parser.add_argument("--reason", default="")
    _ = finish_parser.add_argument("--token", choices=TOKEN_REQUIRED_CHOICES, default="")
    pause_parser = subcommands.add_parser("release-step-memory")
    _ = pause_parser.add_argument("--assignment-file", type=Path, required=True)
    prerequisite_parser = subcommands.add_parser("report-prerequisite")
    _ = prerequisite_parser.add_argument("--assignment-file", type=Path, required=True)
    _ = prerequisite_parser.add_argument("--token", choices=TOKEN_REQUIRED_CHOICES, required=True)
    failed_parser = subcommands.add_parser("claim-failed")
    _ = failed_parser.add_argument("--assignment-file", type=Path, required=True)
    _ = failed_parser.add_argument("--reason", required=True)
    mine_parser = subcommands.add_parser("mine")
    _ = mine_parser.add_argument("session_id")
    for name in ("width", "token-wait"):
        mutation = subcommands.add_parser(name)
        _ = mutation.add_argument("reservation")
        if name != "token-wait":
            _ = mutation.add_argument("value", type=int)
    priority_parser = subcommands.add_parser("priority-set")
    _ = priority_parser.add_argument("unit")
    _ = priority_parser.add_argument("--why", default="the user's bump")
    args = parser.parse_args(argv, namespace=ClientArguments())
    if args.command == "verify-run-slot":
        return _take_verify_run_slot(args.pid, os.getcwd(), args.episode)
    if args.command in ("check-nested", "place-step"):
        reason = nested_reservation_reason()
        if reason:
            if args.pinned_refusals:
                return _print_placement_refusal(reason)
            print(reason, file=sys.stderr)
            return 2
        if args.command == "check-nested":
            return 0
    if args.command == "code-version":
        print(LOADED_ADMISSION_CODE_VERSION)
        return 0
    output = WaitOutput(on_event=(lambda event: print(event, flush=True))
                        if args.wait_events else lambda _event: None)
    retry_token = _ACQUISITION_RETRY.set(AcquisitionRetry(float("inf")))
    try:
        if args.command == "placement-status":
            return _print_placement_status(args.episode, args.attempt)
        if args.command == "down-alert":
            return down_alert()
        if args.command == "finish-step":
            prerequisite = token_prerequisite(args.token) if args.token else KEEP_PLACEMENT_PREREQUISITE
            print(wire.encode_reply(finish_step(args.assignment_file, args.status, args.reason,
                prerequisite=prerequisite)), end="")
            return 0
        if args.command == "release-step-memory":
            assignment = read_assignment(args.assignment_file)
            if not isinstance(assignment, wire.LocalPlacementGranted):
                raise wire.AdmissionUnavailable("release-step-memory requires a local grant")
            answer = release(assignment.grant.reservation, 75, assignment.grant.cleanup_timing)
            if not isinstance(answer, wire.Ok):
                raise wire.AdmissionUnavailable("the service did not confirm the memory release")
            return 0
        if args.command == "report-prerequisite":
            reply = read_assignment(args.assignment_file)
            print(wire.encode_reply(report_prerequisite(wire.PrerequisiteReport(
                uuid.uuid4().hex, reply.assignment.episode, reply.assignment.owner, token_prerequisite(args.token)))), end="")
            return 0
        if args.command == "claim-failed":
            reply = read_assignment(args.assignment_file)
            print(wire.encode_reply(claim_failure(wire.ClaimFailure(
                uuid.uuid4().hex, reply.assignment, args.reason))), end="")
            return 0
        if args.command in ("place-step", "cancel-placement"):
            return _place_step(args, output)
        def command_actor() -> str:
            from scripts.build_queue import admission_records as records
            return records.queue_command_actor()

        result = (view() if args.command == "view" else explain() if args.command == "explain" else
                  mine(args.session_id) if args.command == "mine" else width(args.reservation, args.value)
                  if args.command == "width" else token_wait(args.reservation, saved_cleanup_timing()) if args.command == "token-wait" else
                  priority_set(args.unit, args.why, command_actor()) if args.command == "priority-set" else
                  priority_clear(command_actor()))
        if isinstance(result, wire.Refused):
            return 2
        print(wire.encode_record(result))
    except _QueueLeft:
        output.finished(left=True)
        if args.shell_state is not None:
            _ = args.shell_state.write_text("BUILDLOG_MEM_OUTCOME=LeftQueue\nBUILDLOG_MEM_WAIT_S="
                + str(output.elapsed_seconds()) + "\n")
        return 75
    except (_QueueUnreachable, wire.AdmissionUnavailable, ValueError, OSError) as error:
        if isinstance(error, wire.AdmissionUnavailable):
            reason = f"admission service unavailable: {error.reason}"
            if args.command == "place-step" and not args.pinned_refusals:
                reason += "; step refused"
        elif isinstance(error, _QueueUnreachable):
            reason = str(error)
        else:
            reason = str(error) or "checker returned an error"
        if args.pinned_refusals:
            return _print_placement_refusal(reason)
        print(reason, file=sys.stderr)
        return 2
    finally:
        if output.held and args.command not in ("place-step", "cancel-placement"):
            output.on_event("done")
        _ACQUISITION_RETRY.reset(retry_token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

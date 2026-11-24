#!/usr/bin/env python3
"""Keep one hold per staff member and decide when builds may resume."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import fcntl
import json
import os
import pwd
import re
import shlex
import subprocess
import sys
import time
import threading
import uuid
from collections import Counter
from collections.abc import Callable, Collection, Generator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, time as ClockTime, timedelta, timezone
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NotRequired, Protocol, TextIO, TypedDict, cast, get_args
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.boot_identity import boot_identity
from scripts.lib.process_failure import failure_detail
from scripts.lib import conf, process_start
from scripts.lib.user_zone import user_zone
from scripts.build_hold import owning_session as session_owner
from scripts.build_hold import override_guard
from scripts.build_hold.override_guard import require_hold_write as require_hold_write
from scripts.build_hold.shared_state_defaults import (
    DEFAULT_HOLDER_DIRECTORY as DEFAULT_HOLDER_DIRECTORY,
    DEFAULT_RELEASE_DIRECTORY as DEFAULT_RELEASE_DIRECTORY,
    DEFAULT_PROC_ROOT,
    StopConfirmation,
    StopStatus as StopStatus,
    holder_directory as holder_directory,
    read_stop_confirmation_state,
    release_directory as release_directory,
    stop_confirmation_path as stop_confirmation_path,
    write_stop_confirmation,
)
from scripts.build_targets.target_config import ConfigError, SshTarget, command_target_name, read_targets

from scripts.message import session_send
from scripts.message.send_result import SEND_ABSENT_EXIT, SEND_CLOSED_EXIT

if TYPE_CHECKING:
    from scripts.production import staff


BUILD_COMMANDS = frozenset({"cargo", "rustc", "cargo-nextest"})
PROC_ROOT = DEFAULT_PROC_ROOT
INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})")
SAFE = re.compile(r"[^A-Za-z0-9._-]")
# Retry a failed session lookup before reporting an undelivered release notice.
LOOKUP_TRIES = 3
LOOKUP_RETRY_S = 1.0
# One notice names at most this many stopped runs. Linux limits one argv string to 128 KiB.
RELEASE_NOTICE_RUN_LIMIT = 500
RELEASE_NOTICE = ("/build:hold: the build hold has been released. Start your builds and BRP launches now; "
                  "their memory gate orders them. No answer is needed.")

DeliveryOutcome = Literal[
    "NotAttempted", "Delivered", "QueuedUnconfirmed", "RecipientGone", "WorkerClosed", "DeliveryFailed",
]
DELIVERY_OUTCOMES = frozenset(cast(tuple[object, ...], get_args(DeliveryOutcome)))


class StoppedRun(TypedDict):
    id: str
    label: str
    staff_id: str
    director_dir: str
    pid: NotRequired[int]
    proc_start: NotRequired[str]
    boot_id: NotRequired[str]
    record_path: NotRequired[str]
    owner_session: NotRequired[str]


class ReleaseEntry(TypedDict):
    session_id: str
    name: str
    delivery: DeliveryOutcome
    run: NotRequired[StoppedRun]




class StopRequest(TypedDict):
    id: str
    entry: ReleaseEntry
    runner_record: NotRequired[str]


class HoldCycle(TypedDict):
    id: str
    opened_at: str
    holders: dict[str, dict[str, str]]
    recipients: dict[str, str]
    entries: list[ReleaseEntry]
    release_started_at: str
    stop_logs: NotRequired[list[str]]
    independent_waits: NotRequired[dict[str, bool]]


class ReleaseRecordReadError(ValueError):
    """The stored hold cycle cannot be read safely."""


@dataclass(frozen=True)
class NoCycle:
    pass


@dataclass(frozen=True)
class NoStaffRead:
    """Status has not read the staff snapshot yet."""


class CycleReleaseNotices(TypedDict):
    kind: Literal["cycle"]
    cycle_id: str
    recipients: dict[str, str]
    runs: NotRequired[list[ReleaseEntry]]
    stop_requests: NotRequired[list[StopRequest]]
    stop_waits: NotRequired[list[str]]


class BroadcastReleaseNotice(TypedDict):
    kind: Literal["broadcast"]


class ReleaseIntent(TypedDict):
    holder_key: str
    holder_name: str
    holder_purpose: NotRequired[str]
    cycle_holders: NotRequired[dict[str, dict[str, str]]]
    holder_since: str
    holder_directory: str
    released_at: str
    notices: CycleReleaseNotices | BroadcastReleaseNotice


@dataclass(frozen=True)
class NoReleaseIntent:
    pass


@dataclass(frozen=True)
class RecordedRelease:
    path: Path
    intent: ReleaseIntent
    state: Literal["pending", "completed"]


@dataclass(frozen=True)
class DamagedRecordSetAside:
    path: Path


@dataclass(frozen=True)
class ReleaseRecordsForChange:
    records: list[RecordedRelease]
    set_aside: list[DamagedRecordSetAside]


@dataclass(frozen=True)
class CycleNoticesToSend:
    cycle: HoldCycle
    entries: list[ReleaseEntry]
    unresolved_stops: int
    # Queued notices arrive when their session returns. A release does not send them again.
    queued: list[ReleaseEntry]
    absent_confirmations: list[ReleaseEntry]


class ReleaseNoticeReadError(ReleaseRecordReadError):
    path: Path

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(reason)
        self.path = path


def release_record_error_line(error: ReleaseRecordReadError) -> str:
    return f"release record could not be read: {error}; /build:hold release sets it aside and ends the hold"


@dataclass(frozen=True)
class RecipientGone:
    pass


@dataclass(frozen=True)
class RecipientLive:
    socket: str


@dataclass(frozen=True)
class RecipientLookupFailed:
    reason: str


@dataclass(frozen=True)
class ReleaseDelivered:
    pass


@dataclass(frozen=True)
class ReleaseQueued:
    pass


@dataclass(frozen=True)
class ReleaseWorkerClosed:
    """The sender found a closed or stopped worker with no route."""


@dataclass(frozen=True)
class ReleaseSendFailed:
    reason: str


@dataclass(frozen=True)
class RestartNoticeChecks:
    released_at: datetime
    max_age: timedelta
    clock: Callable[[], datetime]
    cycle_id: str
    newest: dict[str, str]

    def expired(self) -> bool:
        return self.clock() - self.released_at > self.max_age


@dataclass(frozen=True)
class HolderMessageSent:
    """The sender delivered or queued the holder's message."""


@dataclass(frozen=True)
class HolderMessageFailed:
    """The sender could not deliver or queue the holder's message."""
    reason: str


@dataclass(frozen=True)
class KnownReleaseEta:
    at: datetime


@dataclass(frozen=True)
class UnknownReleaseEta:
    pass


ReleaseEta = KnownReleaseEta | UnknownReleaseEta


@dataclass(frozen=True)
class CurrentTime:
    """Read the current time at the rendering entry point."""


CURRENT_TIME = CurrentTime()


@dataclass(frozen=True)
class NoReleaseEta:
    pass


@dataclass(frozen=True)
class ClockReleaseEta:
    clock: ClockTime
    zone: ZoneInfo


@dataclass(frozen=True)
class DatedReleaseEta:
    """The request names a release date and time in its zone."""
    when: datetime


ReleaseRequest = NoReleaseEta | ClockReleaseEta | DatedReleaseEta


@dataclass(frozen=True)
class HolderIdentity:
    key: str
    name: str


@dataclass(frozen=True)
class NatedevHoldTarget:
    pass


@dataclass(frozen=True)
class MacHoldTarget:
    pass


@dataclass(frozen=True)
class ConfiguredSshHoldTarget:
    name: str


@dataclass(frozen=True)
class UnknownHoldTarget:
    name: str
    configured: tuple[str, ...]


@dataclass(frozen=True)
class UncheckedHoldTarget:
    name: str
    reason: str


HoldTarget = NatedevHoldTarget | MacHoldTarget | ConfiguredSshHoldTarget | UnknownHoldTarget | UncheckedHoldTarget


def parse_hold_target(name: str) -> HoldTarget:
    if name == "natedev":
        return NatedevHoldTarget()
    if name == "mac":
        return MacHoldTarget()
    try:
        configured = read_targets()
    except (ConfigError, OSError, UnicodeError) as error:
        return UncheckedHoldTarget(name, str(error))
    for target in configured:
        if target.name == name and isinstance(target, SshTarget):
            return ConfiguredSshHoldTarget(name)
    return UnknownHoldTarget(name, tuple(target.name for target in configured))


@dataclass(frozen=True)
class MacHoldResult:
    status: int
    stdout: str
    stderr: str


def run_mac_hold(arguments: Sequence[str], target: str = "mac") -> MacHoldResult:
    script = Path(__file__).resolve().parent.parent / "mac_test" / "mac_test.py"
    result = subprocess.run([str(Path(__file__).resolve().parents[1] / "lib/py"), str(script), *arguments, "--target", target], capture_output=True, text=True)
    return MacHoldResult(result.returncode, result.stdout, result.stderr)


def release_mac_hold(identity: HolderIdentity, target: str = "mac") -> MacHoldResult:
    return run_mac_hold(["unblock", "--holder", identity.key], target)


def print_mac_hold_result(result: MacHoldResult) -> int:
    print(result.stdout, end="")
    print(result.stderr, end="", file=sys.stderr)
    return result.status


@dataclass(frozen=True)
class Holder:
    # The current staff name, or the text given for a holder outside staff.
    name: str
    since: datetime
    purpose: str
    release: ReleaseEta
    # The fixed staff ID, or the text given for a holder outside staff.
    key: str
    keep: tuple[ProcessIdentity, ...] = ()
    start_recorded: bool = True
    user_words: str = ""

    def display_reason(self) -> str:
        if not self.user_words:
            return self.purpose
        return f"{self.purpose} (Nate's words: {json.dumps(self.user_words, ensure_ascii=False)})"


@dataclass(frozen=True)
class NatedevHolds:
    in_force: tuple[Holder, ...]


@dataclass(frozen=True)
class NoHolders:
    pass


@dataclass(frozen=True)
class ActiveHolders:
    holders: tuple[Holder, ...]

    def __post_init__(self) -> None:
        if not self.holders:
            raise ValueError("active holders cannot be empty")
        if tuple(sorted(self.holders, key=lambda holder: holder.since)) != self.holders:
            raise ValueError("active holders must be ordered by since")


HoldState = NoHolders | ActiveHolders


@dataclass(frozen=True)
class Process:
    pid: int
    user: str
    command: str


@dataclass(frozen=True)
class ProcessCpuTime:
    ticks: int
    started_at_ticks: int


@dataclass(frozen=True)
class CurrentProcessCpu:
    process: Process
    cpu_percent: float


@dataclass(frozen=True)
class Quiet:
    processes: tuple[Process, ...]


@dataclass(frozen=True)
class Busy:
    reasons: tuple[str, ...]


def process_start_time(pid: int, proc_root: Path = PROC_ROOT) -> str:
    if pid <= 0:
        return ""
    try:
        fields = (proc_root / str(pid) / "stat").read_text(encoding="utf-8", errors="replace").rsplit(")", 1)[1].split()
        return fields[19] if fields[0] != "Z" else ""
    except (OSError, IndexError):
        return ""


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    start_time: str


@dataclass(frozen=True)
class LiveClaims:
    """Name the processes that a hold must not stop.

    `routed` names the identities that only a step record routed off natedev claims. A drain does not keep them:
    a later natedev step of that call waits for the release.
    """
    identities: frozenset[ProcessIdentity]
    routed: frozenset[ProcessIdentity] = frozenset()
    remote_waiters: frozenset[ProcessIdentity] = frozenset()


@dataclass(frozen=True)
class ClaimsUnreadable:
    reason: str


class PidNamespaceRelation(Enum):
    """Describe the process PID namespace relative to the auditor."""

    SAME = auto()
    DIFFERENT = auto()
    UNREADABLE = auto()


@dataclass(frozen=True)
class AuditProcess:
    identity: ProcessIdentity
    parent_pid: int
    command: str
    argv: tuple[str, ...]
    cwd: str
    environment: Mapping[str, str]
    in_build_scope: bool
    uid: int = -1
    tty_nr: int = 0
    executable: str = ""
    desktop_app: bool = False
    pid_namespace: PidNamespaceRelation = PidNamespaceRelation.UNREADABLE


@dataclass(frozen=True)
class FullBuildAudit:
    pass


@dataclass(frozen=True)
class DrainBuildAudit:
    pass


@dataclass(frozen=True)
class HoldBuildCandidates:
    uid: int


FULL_BUILD_AUDIT = FullBuildAudit()
DRAIN_BUILD_AUDIT = DrainBuildAudit()
SYSTEM_CGROUP_ROOT = Path("/sys/fs/cgroup")


BuildClass = Literal["holder", "admitted", "claimed", "remote_waiter", "other_session", "no_session"]
AUDIT_COMMANDS = frozenset({"cargo-tile-real", "cargo", "cargo-nextest", "rustc", "rustdoc"})
CARGO_COMMANDS = AUDIT_COMMANDS - {"rustc", "rustdoc"}


@dataclass(frozen=True)
class AuditedBuild:
    process: AuditProcess
    owner: session_owner.OwningSession | session_owner.NoOwningSession
    classification: BuildClass
    children: tuple[ProcessIdentity, ...]


def read_audit_processes(
    proc_root: Path, selection: FullBuildAudit | DrainBuildAudit | HoldBuildCandidates = FULL_BUILD_AUDIT,
    cgroup_root: Path = SYSTEM_CGROUP_ROOT,
) -> dict[int, AuditProcess]:
    """Read process details for the selected audit. Only the drain reads PID namespaces."""
    processes: dict[int, AuditProcess] = {}
    desktop_pids: set[int] = set()
    if isinstance(selection, HoldBuildCandidates):
        cgroup_root = Path(os.environ.get("BUILD_HOLD_CGROUP_ROOT", str(cgroup_root)))
        apps = cgroup_root / f"user.slice/user-{selection.uid}.slice/user@{selection.uid}.service/app.slice"
        for unit in apps.glob("app-*"):
            if not (unit.name.endswith(".scope") or "@" in unit.name and unit.name.endswith(".service")):
                continue
            for file in unit.rglob("cgroup.procs"):
                try:
                    desktop_pids.update(int(pid) for pid in file.read_text().split() if pid.isdecimal())
                except OSError:
                    continue
    for directory in proc_root.iterdir():
        if not directory.name.isdecimal():
            continue
        pid = int(directory.name)
        try:
            stat = (directory / "stat").read_text(errors="replace").rsplit(")", 1)[1].split()
            start = stat[19]
            if stat[0] == "Z" or not start.isdecimal():
                continue
            parent_pid = int(stat[1])
            command = (directory / "comm").read_text(errors="replace").strip()
            uid, tty_nr = directory.stat().st_uid, int(stat[4])
        except (OSError, ValueError, IndexError):
            continue
        try:
            executable = (directory / "exe").readlink().name.removesuffix(" (deleted)")
        except OSError:
            executable = command
        processes[pid] = AuditProcess(ProcessIdentity(pid, start), parent_pid, command, (), "", {}, False,
                                      uid, tty_nr, executable, pid in desktop_pids)
    details: set[int] = set()
    for pid, process in processes.items():
        if (not isinstance(selection, HoldBuildCandidates) or process.uid == selection.uid and
                any(name in AUDIT_COMMANDS | {"pytest", "py.test", "sccache", "bash", "sh", "dash", "zsh",
                                             "verify.sh", "merge_gate.py", "gate.py", "clippy-driver", "rust-analyzer",
                                             "basedpyright", "node", "nodejs"}
                    or name.startswith("python") for name in (process.command, process.executable))):
            details.add(pid)
            details.update(parent.identity.pid for parent in audit_ancestors(process, processes))
    auditor_namespace = None
    if isinstance(selection, DrainBuildAudit):
        try:
            auditor_namespace = (proc_root / "self/ns/pid").readlink()
        except OSError:
            pass
    for pid in details:
        process = processes[pid]
        directory = proc_root / str(pid)
        try:
            argv = tuple(part.decode("utf-8", "replace") for part in
                         (directory / "cmdline").read_bytes().split(b"\0") if part)
        except OSError:
            argv = ()
        try:
            environment = dict(part.decode("utf-8", "replace").split("=", 1) for part in
                               (directory / "environ").read_bytes().split(b"\0") if b"=" in part)
        except OSError:
            environment = {}
        try:
            cwd = str((directory / "cwd").readlink())
        except OSError:
            cwd = ""
        pid_namespace = PidNamespaceRelation.UNREADABLE
        if auditor_namespace is not None:
            try:
                namespace = (directory / "ns/pid").readlink()
                pid_namespace = (PidNamespaceRelation.SAME if namespace == auditor_namespace
                                 else PidNamespaceRelation.DIFFERENT)
            except OSError:
                pass
        in_build_scope = False
        if not isinstance(selection, HoldBuildCandidates):
            try:
                in_build_scope = any("/builds.slice/" in line
                                     and "/builds.slice/builds-detached.slice/" not in line
                                     for line in (directory / "cgroup").read_text(errors="replace").splitlines())
            except OSError:
                pass
        if process_start_time(pid, proc_root) != process.identity.start_time:
            del processes[pid]
            continue
        processes[pid] = AuditProcess(process.identity, process.parent_pid, process.command, argv, cwd,
                                      environment, in_build_scope, process.uid, process.tty_nr,
                                      process.executable, process.desktop_app, pid_namespace=pid_namespace)
    return processes


def audit_ancestors(process: AuditProcess, processes: Mapping[int, AuditProcess]) -> tuple[AuditProcess, ...]:
    ancestors: list[AuditProcess] = []
    seen = {process.identity.pid}
    while process.parent_pid in processes and process.parent_pid not in seen:
        parent = processes[process.parent_pid]
        # A new process at an old parent PID cannot own the child.
        if int(parent.identity.start_time) > int(process.identity.start_time):
            break
        ancestors.append(parent)
        seen.add(parent.identity.pid)
        process = parent
    return tuple(ancestors)


def audit_command(process: AuditProcess) -> str:
    executable = Path(process.argv[0]).name if process.argv else process.command
    return executable if executable in AUDIT_COMMANDS | {"claude", "codex"} else process.command


@dataclass(frozen=True)
class RunAgent:
    """The agent that runs this command receives its release notice."""
    session_id: str


@dataclass(frozen=True)
class NoRunAgent:
    """The command and its ancestors identify no agent."""


def run_agent(process: AuditProcess, ancestors: Sequence[AuditProcess]) -> RunAgent | NoRunAgent:
    """Find the nearest agent independently of the unit director that owns the run."""
    for item in (process, *ancestors):
        thread_id = item.environment.get("CODEX_THREAD_ID", "")
        if thread_id:
            return RunAgent(f"codex:{thread_id}")
        session_id = item.environment.get("CLAUDE_CODE_SESSION_ID", "")
        if session_id:
            return RunAgent(session_id)
    return NoRunAgent()


def audit_owner(process: AuditProcess, ancestors: Sequence[AuditProcess]) -> session_owner.OwningSession | session_owner.NoOwningSession:
    for item in (process, *ancestors):
        session_id = item.environment.get("CLAUDE_CODE_SESSION_ID", "")
        if session_id:
            return session_owner.OwningSession(session_id)
        owner = session_owner.owning_session(item.environment)
        if isinstance(owner, session_owner.OwningSession):
            return owner
        if item.environment.get("CODEX_THREAD_ID"):
            return session_owner.OwningSession(item.environment["CODEX_THREAD_ID"])
    for parent in ancestors:
        if audit_command(parent) in {"claude", "codex"}:
            owner = session_owner.owning_session(parent.environment)
            if isinstance(owner, session_owner.OwningSession):
                return owner
            return session_owner.OwningSession(parent.environment.get("CODEX_THREAD_ID")
                                 or f"{audit_command(parent)}:{parent.identity.pid}:{parent.identity.start_time}")
    return session_owner.NoOwningSession()


def owner_is_sole_holder(owner: session_owner.OwningSession | session_owner.NoOwningSession, holders: Sequence[Holder]) -> bool:
    return (isinstance(owner, session_owner.OwningSession) and bool(holders)
            and all(holder.key == holder_path(Path(), holder_of(owner.session_id).key).name for holder in holders))


class HoldEnforcer(Protocol):
    def admitted_processes(self) -> frozenset[ProcessIdentity]: ...
    def claimed_processes(self, proc_root: Path = PROC_ROOT) -> LiveClaims | ClaimsUnreadable: ...
    def holds_claim(self, process: AuditProcess, processes: Mapping[int, AuditProcess],
                    claimed: frozenset[ProcessIdentity]) -> bool: ...
    def start(self) -> None: ...
    def clear_queued_runs(self, proc_root: Path) -> None: ...
    def record_clear_cutoff(self, identity: HolderIdentity, since: datetime) -> None: ...
    def drain_identities(self, proc_root: Path, identity: HolderIdentity) -> tuple[ProcessIdentity, ...]: ...
    def wait_for_kept_runs(self, holder: Holder, proc_root: Path) -> int: ...
    def ci_placement_lines(self, proc_root: Path) -> list[str]: ...
    def record_build(self, build: AuditedBuild, holder: Holder, proc_root: Path,
                     action: Literal["audit", "stopped"]) -> None: ...
    def report(self, proc_root: Path, directory: Path, *, drain: bool = False) -> list[str]: ...


def hold_enforcer() -> HoldEnforcer:
    # The enforcer uses these audit types; load it after this module is ready.
    import importlib
    return cast(HoldEnforcer, cast(object, importlib.import_module('scripts.build_hold.hold_enforcer')))


def audit_builds(proc_root: Path, holders: Sequence[Holder]) -> tuple[AuditedBuild, ...]:
    processes = read_audit_processes(proc_root)
    admitted = hold_enforcer().admitted_processes()
    claims = hold_enforcer().claimed_processes(proc_root) if holders else LiveClaims(frozenset())
    if isinstance(claims, ClaimsUnreadable):
        print(f"claims unreadable: {claims.reason}", file=sys.stderr)
    # A remote waiter does not grant its local build children an audit exemption.
    claimed: frozenset[ProcessIdentity] = (claims.identities - claims.remote_waiters
        if isinstance(claims, LiveClaims) else frozenset())
    builds: list[AuditedBuild] = []
    children: dict[int, list[ProcessIdentity]] = {}
    roots: list[AuditProcess] = []
    for process in processes.values():
        if audit_command(process) not in AUDIT_COMMANDS:
            continue
        ancestors = audit_ancestors(process, processes)
        cargo_parents = [parent for parent in ancestors if audit_command(parent) in CARGO_COMMANDS]
        if cargo_parents:
            children.setdefault(cargo_parents[-1].identity.pid, []).append(process.identity)
        else:
            roots.append(process)
    for process in sorted(roots, key=lambda item: item.identity.pid):
        ancestors = audit_ancestors(process, processes)
        if any(process_start_time(item.identity.pid, proc_root) != item.identity.start_time
               for item in (process, *ancestors)):
            continue
        owner = audit_owner(process, ancestors)
        classification: BuildClass
        if (isinstance(claims, LiveClaims)
                and process.identity in claims.remote_waiters):
            classification = "remote_waiter"
        elif hold_enforcer().holds_claim(process, processes, claimed):
            classification = "claimed"
        elif owner_is_sole_holder(owner, holders):
            classification = "holder"
        elif process.in_build_scope or any(item.identity in admitted for item in (process, *ancestors)):
            classification = "admitted"
        elif isinstance(owner, session_owner.OwningSession):
            classification = "other_session"
        else:
            classification = "no_session"
        live_children = tuple(sorted((child for child in children.get(process.identity.pid, [])
                                      if process_start_time(child.pid, proc_root) == child.start_time),
                                     key=lambda item: item.pid))
        builds.append(AuditedBuild(process, owner, classification, live_children))
    return tuple(builds)


def audit_lines(builds: Sequence[AuditedBuild], *, beginning: bool = False) -> list[str]:
    lines = ["Build audit: tests and builds outside the hold are stopped; use --keep <pid> to keep a run."
             if beginning else "Build audit:"]
    for build in builds:
        if build.classification == "no_session":
            continue
        owner = build.owner.session_id if isinstance(build.owner, session_owner.OwningSession) else ""
        command = shlex.join(build.process.argv) or build.process.command
        reason = "holds a live Mac or AWS claim" if build.classification == "claimed" else build.classification
        lines.append(f"  {reason}: {owner}, pid {build.process.identity.pid}: {command}"
                     + (f" ({len(build.children)} build children)" if build.children else ""))
    if len(lines) == 1:
        lines.append("  No session-owned builds are running.")
    return lines


def audit_hold(directory: Path, proc_root: Path) -> tuple[AuditedBuild, ...]:
    try:
        holders = natedev_holds(directory).in_force
    except ReleaseRecordReadError:
        state = read_holders(directory)
        holders = state.holders if isinstance(state, ActiveHolders) else ()
    builds = audit_builds(proc_root, holders)
    for build in builds:
        if build.classification in {"holder", "admitted", "claimed", "remote_waiter"}:
            continue
        for holder in holders:
            hold_enforcer().record_build(build, holder, proc_root, "audit")
    return builds


def print_hold_audit(directory: Path, proc_root: Path, *, beginning: bool = False, drain: bool = False) -> None:
    try:
        builds = audit_hold(directory, proc_root)
    except (OSError, ValueError) as error:
        print(f"Build audit unavailable: {error}")
        return
    print("\n".join(audit_lines(builds, beginning=beginning)))
    try:
        for line in hold_enforcer().report(proc_root, directory, drain=drain):
            print(line)
    except (OSError, ValueError) as error:
        print(f"Hold enforcer report unavailable: {error}")


def holder_path(directory: Path, name: str) -> Path:
    safe = SAFE.sub("-", name)
    if not safe or safe in {".", ".."}:
        raise ValueError("holder name must contain a file-name character")
    return directory / safe


def holder_of(given: str | None) -> HolderIdentity:
    """Resolve a fixed staff key and its current display name."""
    from scripts.production import staff

    try:
        membership = staff.snapshot()
    except staff.StaffAbsent:
        membership = staff.StaffSnapshot(())
    if given is None:
        own = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
        member = membership.by_current_session(own) if own else None
        if member is None:
            raise ValueError("give --holder: this caller has no staff row")
        return HolderIdentity(member.id, member.name)
    from scripts.message.sessions import staff_recipient

    member = membership.by_member_reference(given)
    resolved = member if member is not None else staff_recipient(given, membership)
    return HolderIdentity(resolved.id, resolved.name) if isinstance(resolved, staff.StaffRow) else HolderIdentity(given, given)


def holder_address(key: str) -> str:
    """Use a staff ID directly; preserve native session addresses for other keys."""
    from scripts.production import staff

    member = staff.initial_snapshot().by_id(staff.StaffId(key))
    return key if member is not None else f"session:{key}"


def send_holder_message(key: str, text: str) -> HolderMessageSent | HolderMessageFailed:
    """Send one holder message within the configured timeout."""
    from scripts.lib import conf

    try:
        with conf.diagnostics(report=False):
            timeout = conf.read("build_targets").number("hold_sender_timeout_seconds")
        result = subprocess.run(session_send.send_argv("--to", holder_address(key), "--text", text),
                                capture_output=True, text=True, errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired as error:
        return HolderMessageFailed(f"send timed out after {error.timeout:g} seconds")
    except (OSError, conf.Unusable) as error:
        return HolderMessageFailed(str(error))
    if result.returncode in (0, 1):
        return HolderMessageSent()
    return HolderMessageFailed(failure_detail(result))


def holder_session(key: str) -> str:
    """Use a staff holder's current session; keep a native session key as it is."""
    from scripts.production import staff

    member = staff.initial_snapshot().by_id(staff.StaffId(key))
    return member.current_session if member is not None else key


def held_key(directory: Path, given: str | None) -> str:
    """Find only the resolved holder's fixed key."""
    identity = holder_of(given)
    if not holder_path(directory, identity.key).is_file():
        raise ValueError(f"{given or identity.name} held nothing")
    return identity.key


def aware_instant(value: str) -> datetime:
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if instant.utcoffset() is None:
        raise ValueError("instant needs an offset")
    return instant


def read_holder(path: Path) -> Holder:
    """Even an unreadable or malformed regular file remains an active hold."""
    from scripts.production import staff

    fallback = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    try:
        content = path.read_text().strip()
    except FileNotFoundError:
        raise
    except (OSError, UnicodeError):
        return Holder(path.name, fallback, "unreadable holder file", UnknownReleaseEta(), path.name, start_recorded=False)
    try:
        raw = cast(object, json.loads(content))
        if isinstance(raw, dict):
            fields = cast(dict[str, object], raw)
            name = fields.get("holder")
            since = fields.get("since")
            purpose = fields.get("for")
            release = fields.get("release_eta")
            if isinstance(name, str) and isinstance(since, str) and isinstance(purpose, str) and isinstance(release, str):
                eta: ReleaseEta = UnknownReleaseEta() if release == "unknown" else KnownReleaseEta(aware_instant(release))
                member = staff.initial_snapshot().by_id(staff.StaffId(path.name))
                now = member.name if member is not None else name
                keep: list[ProcessIdentity] = []
                values = fields.get("keep", [])
                if isinstance(values, list):
                    for value in cast(list[object], values):
                        if isinstance(value, dict):
                            entry = cast(dict[str, object], value)
                            pid, start = entry.get("pid"), entry.get("start_time")
                            if isinstance(pid, int) and pid > 0 and isinstance(start, str) and start.isdecimal():
                                keep.append(ProcessIdentity(pid, start))
                words = fields.get("user_words")
                return Holder(now, aware_instant(since), purpose, eta, path.name, tuple(keep),
                              user_words=words if isinstance(words, str) and words.strip() else "")
    except (ValueError, TypeError):
        pass
    match = INSTANT.search(content)
    if match is None:
        return Holder(path.name, fallback, content, UnknownReleaseEta(), path.name, start_recorded=False)
    try:
        since = aware_instant(match.group())
    except ValueError:
        return Holder(path.name, fallback, content[match.end():].lstrip(" ,;:-").strip(), UnknownReleaseEta(), path.name,
                      start_recorded=False)
    return Holder(path.name, since, content[match.end():].lstrip(" ,;:-").strip(), UnknownReleaseEta(), path.name)


def read_holders(directory: Path) -> HoldState:
    try:
        paths = [path for path in directory.iterdir() if path.is_file()]
    except FileNotFoundError:
        return NoHolders()
    found: list[Holder] = []
    for path in paths:
        try:
            found.append(read_holder(path))
        except FileNotFoundError:
            continue
    holders = tuple(sorted(found, key=lambda holder: (holder.since, holder.name)))
    return ActiveHolders(holders) if holders else NoHolders()


def has_holder_file(directory: Path) -> bool:
    try:
        return any(path.is_file() for path in directory.iterdir())
    except FileNotFoundError:
        return False


def live_holds(cycle: HoldCycle, directory: Path) -> tuple[Holder, ...]:
    holders = read_holders(directory)
    if isinstance(holders, NoHolders):
        return ()
    return holders_in_force(cycle, directory, holders.holders)


def holders_in_force(cycle: HoldCycle, directory: Path, holders: tuple[Holder, ...]) -> tuple[Holder, ...]:
    # Cycle fields record history. Only removal of a holder file ends a hold.
    _ = cycle
    return tuple(holder for holder in holders if holder_path(directory, holder.key).is_file())


def natedev_holds(directory: Path) -> NatedevHolds:
    holders = read_holders(directory)
    if isinstance(holders, NoHolders):
        return NatedevHolds(())
    return NatedevHolds(holders.holders)


def caller_processes(proc_root: Path, pid: int) -> frozenset[ProcessIdentity]:
    """Read the caller and its live ancestors without scanning other processes."""
    lineage: set[ProcessIdentity] = set()
    seen: set[int] = set()
    child_start = float("inf")
    while pid > 0 and pid not in seen:
        seen.add(pid)
        try:
            fields = (proc_root / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
            start = fields[19]
            if fields[0] == "Z" or int(start) > child_start:
                break
            identity = ProcessIdentity(pid, start)
            if process_start_time(pid, proc_root) != start:
                break
            lineage.add(identity)
            pid, child_start = int(fields[1]), int(start)
        except (OSError, ValueError, IndexError):
            break
    return frozenset(lineage)


def release_eta_text(release: ReleaseEta) -> str:
    if isinstance(release, UnknownReleaseEta):
        return "unknown"
    return release.at.astimezone(user_zone()).strftime("%H:%M %Z")


def release_overdue(release: ReleaseEta, now: datetime) -> bool:
    """A known release is overdue at or after its instant."""
    return (isinstance(release, KnownReleaseEta)
            and release.at.astimezone(timezone.utc) <= now.astimezone(timezone.utc))


def holder_text(holder: Holder, now: datetime | CurrentTime = CURRENT_TIME) -> str:
    instant = datetime.now(timezone.utc) if isinstance(now, CurrentTime) else now
    overdue = ", overdue" if release_overdue(holder.release, instant) else ""
    return f"{holder.name} (for {holder.purpose}, release eta {release_eta_text(holder.release)}{overdue})"


def live_kept_runs(holder: Holder, proc_root: Path = PROC_ROOT) -> tuple[ProcessIdentity, ...]:
    """Count only kept identities whose process start times still match."""
    return tuple(item for item in dict.fromkeys(holder.keep)
                 if process_start_time(item.pid, proc_root) == item.start_time)


def holder_state(holder: Holder, proc_root: Path = PROC_ROOT) -> str:
    remaining = len(live_kept_runs(holder, proc_root))
    return f"draining ({remaining} runs left)" if remaining else "held"


def ci_process(pid: int, start_time: str, proc_root: Path) -> bool:
    """Recognize a CI worker lineage without reading private process details."""
    lineage = caller_processes(proc_root, pid)
    if ProcessIdentity(pid, start_time) not in lineage:
        return False
    for identity in lineage:
        directory = proc_root / str(identity.pid)
        try:
            if (directory / "comm").read_text().strip() == "Runner.Worker":
                return True
        except OSError:
            pass
        try:
            if pwd.getpwuid(directory.stat().st_uid).pw_name == "hana-linux":
                return True
        except (OSError, KeyError):
            pass
    return False


def ci_placement_lines(proc_root: Path) -> list[str]:
    return hold_enforcer().ci_placement_lines(proc_root)


def resume_refusal(holders: tuple[Holder, ...]) -> str:
    now = datetime.now(timezone.utc)
    return ("a hold is in force (" + "; ".join(holder_text(holder, now) for holder in holders)
            + "); --resume only continues a release already begun. Release that hold without --resume.")


def release_request(release_time: str | None, zone_name: str | None) -> ReleaseRequest:
    if release_time is None:
        if zone_name is not None:
            raise ValueError("--zone goes with --release-eta")
        return NoReleaseEta()
    if zone_name is None:
        raise ValueError("--zone goes with --release-eta")
    dated = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d", release_time))
    if not dated and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", release_time):
        raise ValueError("release ETA must be HH:MM or YYYY-MM-DDTHH:MM")
    try:
        zone = ZoneInfo(zone_name)
    except ZoneInfoNotFoundError:
        raise ValueError(f"unknown IANA zone: {zone_name}") from None
    if dated:
        return DatedReleaseEta(datetime.fromisoformat(release_time).replace(tzinfo=zone))
    hour, minute = map(int, release_time.split(":"))
    return ClockReleaseEta(ClockTime(hour, minute), zone)


class KeptRunRecord(TypedDict):
    pid: int
    start_time: str


HoldFileRecord = TypedDict("HoldFileRecord", {
    "holder": str, "since": str, "for": str, "release_eta": str, "keep": list[KeptRunRecord],
    "user_words": str,
})


def require_user_words(user_words: str) -> str:
    if not user_words.strip():
        raise ValueError("a hold requires Nate's explicit request. Give his exact words with --user-words")
    return user_words


def write_hold(directory: Path, identity: HolderIdentity, purpose: str, request: ReleaseRequest, now: datetime,
               keep: Sequence[ProcessIdentity] = (), *,
               user_words: str = "",
               before_publish: Callable[[], None] | None = None) -> str:
    """Keep a hold under its resolved key and display name."""
    words = require_user_words(user_words)
    path = holder_path(directory, identity.key)
    temporary = directory.parent / f"{directory.name}.{path.name}.tmp"
    require_hold_write(directory, temporary)
    if not purpose.strip() or "\n" in purpose:
        raise ValueError("hold purpose must be one line of text")
    if isinstance(request, NoReleaseEta):
        release_eta = "unknown"
    else:
        release = (request.when if isinstance(request, DatedReleaseEta)
                   else datetime.combine(now.astimezone(request.zone).date(), request.clock, request.zone))
        if release.astimezone(timezone.utc) <= now.astimezone(timezone.utc):
            raise ValueError("release ETA has already passed")
        release_eta = release.isoformat()
    directory.mkdir(parents=True, exist_ok=True)
    previous = read_holder(path).keep if path.is_file() else ()
    record: HoldFileRecord = {
        "holder": identity.name, "since": now.isoformat(), "for": purpose.strip(), "release_eta": release_eta,
        "keep": [{"pid": item.pid, "start_time": item.start_time} for item in dict.fromkeys((*previous, *keep))],
        "user_words": words,
    }
    _ = temporary.write_text(json.dumps(record, ensure_ascii=False) + "\n")
    if before_publish is not None:
        before_publish()
    os.replace(temporary, path)
    return (f"natedev is held for {purpose.strip()}. "
            "Existing queued runs and running work outside the allow rules stop. "
            "New natedev work waits until release. "
            "Kept runs and their children continue. Eligible tests can use the Mac. "
            "Stopped sessions get a release notice. Repeat stopped work after release. "
            "No release after 2 h: ask me.")


@dataclass(frozen=True)
class NatedevHoldEvent:
    kind: Literal["began", "ended"]
    holder: Holder
    at: datetime


pending_hold_events: ContextVar[tuple[NatedevHoldEvent, ...]] = ContextVar("pending_hold_events", default=())
coordination_depth: ContextVar[int] = ContextVar("coordination_depth", default=0)


@contextmanager
def record_after_unlock() -> Generator[None, None, None]:
    depth = coordination_depth.get()
    token = coordination_depth.set(depth + 1)
    try:
        yield
    finally:
        coordination_depth.reset(token)
        if depth == 0:
            events = pending_hold_events.get()
            _ = pending_hold_events.set(())
            for event in events:
                start_hold_recorder(event)


def start_hold_recorder(event: NatedevHoldEvent) -> None:
    until = event.holder.release.at.isoformat() if isinstance(event.holder.release, KnownReleaseEta) else ""
    command = [str(Path(__file__).resolve().parents[1] / "lib/py"), str(Path(__file__).resolve().parent.parent / "buildlog" / "record.py"),
               "hold-event", event.kind, "natedev", event.holder.key, event.holder.name, event.holder.purpose,
               event.at.astimezone(timezone.utc).isoformat(), until]
    start_recorder(command)


def start_recorder(command: list[str]) -> None:
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    except OSError:
        return
    threading.Thread(target=process.wait, name="hold-event-recorder", daemon=True).start()


def record_hold_event(event: Literal["began", "ended"], holder: Holder, at: datetime) -> None:
    record = NatedevHoldEvent(event, holder, at)
    if coordination_depth.get():
        _ = pending_hold_events.set((*pending_hold_events.get(), record))
    else:
        start_hold_recorder(record)


def release_hold(directory: Path, holder: Holder) -> str:
    require_hold_write(directory)
    path = directory / holder.key
    now = datetime.now(timezone.utc)
    try:
        path.unlink()
    except FileNotFoundError:
        raise ValueError(f"{holder.name} held nothing") from None
    record_hold_event("ended", holder, now)
    state = read_holders(directory)
    if isinstance(state, NoHolders):
        return "released, builds may resume."
    return "released; still held by " + "; ".join(holder_text(holder, now) for holder in state.holders)


@dataclass(frozen=True)
class ReleaseLockHeld:
    """The release lock covers these validated holder and release directories."""
    holders: Path
    releases: Path
    lock: TextIO

    def require(self, *paths: Path) -> None:
        if self.lock.closed:
            raise ValueError("the release lock is no longer held")
        require_hold_write(*paths)
        if holder_directory().resolve() != self.holders or release_directory().resolve() != self.releases:
            raise ValueError("the release lock directories changed")
        if any(not path.resolve().is_relative_to(self.releases) for path in paths):
            raise ValueError("the release record is outside the locked release directory")


@contextmanager
def release_lock(*directories: Path) -> Generator[ReleaseLockHeld]:
    require_hold_write(*directories)
    directory = release_directory()
    require_hold_write(directory / "release.lock")
    directory.mkdir(parents=True, exist_ok=True)
    with record_after_unlock(), (directory / "release.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield ReleaseLockHeld(holder_directory().resolve(), directory.resolve(), lock)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def cycle_path() -> Path | NoCycle:
    current = release_directory() / "current"
    try:
        cycle_id = current.read_text().strip()
    except FileNotFoundError:
        return NoCycle()
    if not re.fullmatch(r"[0-9a-f]{32}", cycle_id):
        raise ValueError("invalid current hold cycle")
    return release_directory() / cycle_id / "cycle.json"


def read_cycle() -> HoldCycle | NoCycle:
    try:
        path = cycle_path()
    except (OSError, ValueError) as error:
        raise ReleaseRecordReadError(str(error)) from error
    return path if isinstance(path, NoCycle) else read_hold_cycle(path)


def read_stopped_run(value: object) -> StoppedRun:
    match value:
        case {"id": str(run_id), "label": str(label), "staff_id": str(staff_id), "director_dir": str(directory)} if run_id and label:
            run: StoppedRun = {"id": run_id, "label": label, "staff_id": staff_id, "director_dir": directory}
            if isinstance(value, dict):
                fields = cast(dict[str, object], value)
                match fields:
                    case {"pid": int(pid), "proc_start": str(start), "boot_id": str(boot)} if pid > 0 and start and boot:
                        run.update(pid=pid, proc_start=start, boot_id=boot)
                    case _:
                        pass
                if isinstance(path := fields.get("record_path"), str) and Path(path).is_absolute():
                    run["record_path"] = path
                # Old staff-only captures did not record the command's session.
                if isinstance(owner := fields.get("owner_session"), str) and owner and fields.get("owner_from_staff") is not True:
                    run["owner_session"] = owner
            return run
        case _:
            raise ValueError("invalid stopped run in release record")


def read_hold_cycle(path: Path) -> HoldCycle:
    """Read current and retained cycles through the same validation boundary."""
    try:
        record = cast(object, json.loads(path.read_text()))
        if not isinstance(record, dict):
            raise ValueError("invalid hold cycle: expected object")
        cycle = cast(dict[str, object], record)
        for field in ("id", "opened_at", "release_started_at"):
            if not isinstance(cycle.get(field), str):
                raise ValueError(f"invalid {field}: expected string")
        holders = cycle.get("holders")
        if not isinstance(holders, dict):
            raise ValueError("invalid holders: expected object")
        for name, instants in cast(dict[object, object], holders).items():
            if not isinstance(name, str) or not isinstance(instants, dict):
                raise ValueError("invalid holder: expected name and object")
            fields = cast(dict[str, object], instants)
            if not isinstance(fields.get("since"), str) or not isinstance(fields.get("released_at"), str):
                raise ValueError(f"invalid holder {name}: expected since and released_at strings")
            _ = aware_instant(cast(str, fields["since"]))
        recipients = cycle.get("recipients")
        if not isinstance(recipients, dict):
            raise ValueError("invalid recipients: expected object")
        if any(not isinstance(name, str) or not isinstance(session_id, str)
               for session_id, name in cast(dict[object, object], recipients).items()):
            raise ValueError("invalid recipients: expected string names and session IDs")
        independent_waits = cycle.get("independent_waits", {})
        if not isinstance(independent_waits, dict) or any(
            not isinstance(session, str) or not isinstance(independent, bool)
            for session, independent in cast(dict[object, object], independent_waits).items()
        ):
            raise ValueError("invalid wait registrations")
        if "stop_logs" in cycle:
            logs = cycle["stop_logs"]
            if not isinstance(logs, list) or any(
                not isinstance(name, str) or not re.fullmatch(r"hold-builds-[0-9a-f]{64}\.jsonl", name)
                for name in cast(list[object], logs)
            ):
                raise ValueError("invalid hold stop logs")
        entries = cycle.get("entries")
        if not isinstance(entries, list):
            raise ValueError("invalid entries: expected list")
        normalized: list[ReleaseEntry] = []
        for index, value in enumerate(cast(list[object], entries)):
            if not isinstance(value, dict):
                raise ValueError(f"invalid entry {index}: expected object")
            entry = cast(dict[str, object], value)
            session_id, name = entry.get("session_id"), entry.get("name")
            if not isinstance(session_id, str) or not isinstance(name, str):
                raise ValueError(f"invalid entry {index}: expected session ID and name")
            old_state = entry.get("state")
            old_outcomes = {"DeliveryFailed": "DeliveryFailed", "RecipientGone": "RecipientGone",
                            "DeliveryQueued": "QueuedUnconfirmed", "ReleasedAwaitingAdmission": "Delivered",
                            "NoAdmissionAck": "Delivered", "WaitingForMemory": "Delivered",
                            "MemoryGateReturned": "Delivered"}
            delivery = entry.get("delivery", old_outcomes.get(str(old_state), "NotAttempted"))
            # Old cycles keep delivery by holder generation and include call and gate fields.
            if isinstance(delivery, dict):
                old_delivery = cast(dict[str, object], delivery)
                delivery = (old_delivery.get("outcome", "NotAttempted")
                            if old_delivery.get("generation") == cycle.get("holder_generation", 1)
                            else "NotAttempted")
            if delivery not in DELIVERY_OUTCOMES:
                raise ValueError(f"invalid delivery for entry {index}")
            item: ReleaseEntry = {"session_id": session_id, "name": name, "delivery": cast(DeliveryOutcome, delivery)}
            if "run" in entry:
                item["run"] = read_stopped_run(entry["run"])
            normalized.append(item)
        cycle["entries"] = normalized
        return cast(HoldCycle, cast(object, cycle))
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise ReleaseRecordReadError(str(error)) from error


def set_aside_damaged_record(held: ReleaseLockHeld, path: Path) -> DamagedRecordSetAside:
    """Preserve a damaged record under release.lock without replacing an earlier copy."""
    held.require(path.parent)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = path.with_name(f"{path.name}.damaged-{timestamp}")
    suffix = 2
    while destination.exists() or destination.is_symlink():
        destination = path.with_name(f"{path.name}.damaged-{timestamp}-{suffix}")
        suffix += 1
    held.require(destination)
    _ = path.rename(destination)
    return DamagedRecordSetAside(destination)


def read_cycle_for_change(held: ReleaseLockHeld) -> HoldCycle | NoCycle | DamagedRecordSetAside:
    """Read a cycle while release.lock is held, setting damage aside if found."""
    held.require()
    try:
        return read_cycle()
    except ReleaseRecordReadError as error:
        current = release_directory() / "current"
        try:
            path = cycle_path()
        except (OSError, ValueError):
            path = NoCycle()
        damaged = path if isinstance(path, Path) and (path.is_symlink() or path.exists()) else current
        saved = set_aside_damaged_record(held, damaged)
        current.unlink(missing_ok=True)
        print(f"release record {damaged} could not be read ({error}); set aside as {saved.path}; this hold now releases every session at once", flush=True)
        return saved


def save_cycle(cycle: HoldCycle) -> None:
    path = release_directory() / cycle["id"] / "cycle.json"
    temporary = path.with_suffix(".tmp")
    require_hold_write(path, temporary)
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = temporary.write_text(json.dumps(cycle, ensure_ascii=False) + "\n")
    os.replace(temporary, path)


def read_release_intent(path: Path) -> ReleaseIntent | NoReleaseIntent:
    try:
        raw = cast(object, json.loads(path.read_text()))
    except FileNotFoundError:
        return NoReleaseIntent()
    if not isinstance(raw, dict):
        raise ValueError("invalid release intent: expected object")
    fields = cast(dict[str, object], raw)
    for field in ("holder_key", "holder_name", "holder_since", "holder_directory", "released_at"):
        if not isinstance(fields.get(field), str) or not fields[field]:
            raise ValueError(f"invalid release intent: {field}")
    _ = aware_instant(cast(str, fields["holder_since"]))
    _ = aware_instant(cast(str, fields["released_at"]))
    notices = fields.get("notices")
    if not isinstance(notices, dict):
        raise ValueError("invalid release intent: notices")
    notice_fields = cast(dict[str, object], notices)
    if notice_fields.get("kind") == "cycle":
        cycle_id, recipients = notice_fields.get("cycle_id"), notice_fields.get("recipients")
        if not isinstance(cycle_id, str) or not re.fullmatch(r"[0-9a-f]{32}", cycle_id):
            raise ValueError("invalid release intent: cycle ID")
        if not isinstance(recipients, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in cast(dict[object, object], recipients).items()
        ):
            raise ValueError("invalid release intent: recipients")
        stop_waits = notice_fields.get("stop_waits", [])
        if not isinstance(stop_waits, list) or any(
            not isinstance(session, str) for session in cast(list[object], stop_waits)
        ):
            raise ValueError("invalid release intent: stop registrations")
        runs = notice_fields.get("runs", [])
        if not isinstance(runs, list):
            raise ValueError("invalid release intent: stopped runs")
        for entry in cast(list[object], runs):
            _ = read_restart_entry(entry)
        requests = notice_fields.get("stop_requests", [])
        if not isinstance(requests, list):
            raise ValueError("invalid release intent: stop requests")
        for request in cast(list[object], requests):
            fields_by_name = cast(dict[str, object], request) if isinstance(request, dict) else {}
            match fields_by_name:
                case {"id": str(request_id), "entry": entry} if re.fullmatch(r"[0-9a-f]{32}", request_id):
                    _ = read_restart_entry(entry)
                    runner_record = fields_by_name.get("runner_record", "")
                    if not isinstance(runner_record, str) or runner_record and not Path(runner_record).is_absolute():
                        raise ValueError("invalid release intent: runner record path")
                case _:
                    raise ValueError("invalid release intent: stop request")
    elif notice_fields.get("kind") != "broadcast":
        raise ValueError("invalid release intent: notice kind")
    return cast(ReleaseIntent, cast(object, fields))


def save_release_intent(intent: ReleaseIntent) -> None:
    path = release_directory() / "pending-release.json"
    require_hold_write(path, Path(intent["holder_directory"]))
    try:
        previous = read_release_intent(path)
    except (OSError, ValueError):
        previous = NoReleaseIntent()
    notices = intent["notices"]
    same_cycle = (not isinstance(previous, NoReleaseIntent) and notices["kind"] == "cycle"
                  and previous["notices"]["kind"] == "cycle"
                  and previous["notices"]["cycle_id"] == notices["cycle_id"]
                  and previous["holder_directory"] == intent["holder_directory"])
    temporary = path.with_suffix(".tmp")
    _ = temporary.write_text(json.dumps(intent, ensure_ascii=False) + "\n")
    if not same_cycle:
        retain_release_record(path)
    os.replace(temporary, path)


def retain_release_record(path: Path) -> None:
    """Move a prior pending release out of the current slot without dropping its notices."""
    require_hold_write(path)
    if path.is_file():
        destination = release_directory() / "releases" / uuid.uuid4().hex / path.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(path, destination)


def complete_release_record(held: ReleaseLockHeld, record: RecordedRelease) -> None:
    """Keep the holder's latest release receipt after its notices are done."""
    receipt = holder_path(release_directory() / "receipts", record.intent["holder_key"]) / "completed-release.json"
    held.require(record.path, receipt)
    require_hold_write(Path(record.intent["holder_directory"]))
    receipt.parent.mkdir(parents=True, exist_ok=True)
    previous = read_release_intent(receipt)
    if (isinstance(previous, NoReleaseIntent)
            or aware_instant(previous["released_at"]) <= aware_instant(record.intent["released_at"])):
        os.replace(record.path, receipt)
    else:
        # An older pending release can finish after a newer release for this holder.
        record.path.unlink()
    if record.path.parent.parent == release_directory() / "releases":
        try:
            record.path.parent.rmdir()
        except OSError:
            # A directory that still contains another record must stay.
            pass


def pending_release_paths() -> list[Path]:
    return [release_directory() / "pending-release.json",
            *sorted((release_directory() / "releases").glob("*/pending-release.json"))]


def first_release_entries(cycle: HoldCycle) -> dict[str, ReleaseEntry]:
    """Use the first recorded entry for each session."""
    first: dict[str, ReleaseEntry] = {}
    for entry in cycle["entries"]:
        if "run" not in entry:
            _ = first.setdefault(entry["session_id"], entry)
    return first


def stopped_run_entries(entries: Sequence[ReleaseEntry]) -> dict[str, ReleaseEntry]:
    runs: dict[str, ReleaseEntry] = {}
    for entry in entries:
        if "run" in entry:
            _ = runs.setdefault(entry["run"]["id"], entry)
    return runs


def release_entries(cycle: HoldCycle, recipients: Mapping[str, str],
                    saved_runs: Sequence[ReleaseEntry] = ()) -> list[ReleaseEntry]:
    first = first_release_entries(cycle)
    runs = stopped_run_entries([*cycle["entries"], *(read_restart_entry(entry) for entry in saved_runs)])
    notified = {entry["session_id"].removeprefix("codex:") for entry in runs.values()
                if entry["delivery"] in ("Delivered", "QueuedUnconfirmed")}
    return [*runs.values(), *(first[session_id] if session_id in first else new_entry(session_id, name)
                             for session_id, name in recipients.items() if session_id.removeprefix("codex:") not in notified)]


def release_notice_pending(entry: ReleaseEntry) -> bool:
    """Keep a stopped run pending if neither recipient took the notice."""
    delivery = entry["delivery"]
    return (delivery in ("NotAttempted", "DeliveryFailed", "WorkerClosed")
            or delivery == "RecipientGone" and ("run" in entry or entry["session_id"].startswith("codex:")))


def read_release_cycle(notices: CycleReleaseNotices) -> HoldCycle:
    path = release_directory() / notices["cycle_id"] / "cycle.json"
    try:
        cycle = read_hold_cycle(path)
        if cycle["id"] != notices["cycle_id"]:
            raise ValueError("release intent does not match its hold cycle")
    except (OSError, ValueError) as error:
        raise ReleaseNoticeReadError(path, str(error)) from error
    return cycle


def release_notices(intent: ReleaseIntent) -> CycleNoticesToSend | BroadcastReleaseNotice:
    """Read and select a release's unsent entries without changing its files or cycle."""
    notices = intent["notices"]
    if notices["kind"] == "broadcast":
        return notices
    cycle = read_release_cycle(notices)
    runs = list(notices.get("runs", []))
    unresolved = 0
    absent_confirmations: list[ReleaseEntry] = []
    for request in notices.get("stop_requests", []):
        match stop_request_status(request):
            case "stopped":
                runs.append(request["entry"])
            case "absent":
                absent_confirmations.append(request["entry"])
                unresolved += 1
            case "unreadable":
                unresolved += 1
            case "not_stopped":
                pass
    entries = release_entries(cycle, notices["recipients"], runs)
    return CycleNoticesToSend(cycle, [entry for entry in entries if release_notice_pending(entry)], unresolved,
                              [entry for entry in entries if entry["delivery"] == "QueuedUnconfirmed"],
                              absent_confirmations)


def entry_name(entry: ReleaseEntry, recipients: Mapping[str, str], membership: staff.StaffSnapshot | None) -> str:
    """Use the staff name when a staff row matches. Otherwise use the recorded name."""
    run = entry.get("run")
    if membership is not None:
        session = entry["session_id"].removeprefix("codex:")
        row = (membership.by_current_session(session) if run is not None
               else membership.by_member_reference(session))
        if row is not None:
            return row.name
    return entry["name"] if run is not None else recipients.get(entry["session_id"], entry["name"])


def recipients_text(entries: Sequence[ReleaseEntry], recipients: Mapping[str, str],
                    membership: staff.StaffSnapshot | None) -> str:
    """Name each recipient session once, with the count of its stopped runs."""
    sessions: dict[str, tuple[str, int]] = {}
    for entry in entries:
        name, runs = sessions.get(entry["session_id"]) or (entry_name(entry, recipients, membership), 0)
        sessions[entry["session_id"]] = (name, runs + int("run" in entry))
    return ", ".join(f"{name} ({runs} stopped run{'s' if runs > 1 else ''})" if runs else name
                     for name, runs in sessions.values()) or "none"


def pending_release_status_lines() -> list[str]:
    """Name unfinished notices without locking, migrating or changing release state."""
    from scripts.production import staff

    lines: list[str] = []
    membership: staff.StaffSnapshot | None | NoStaffRead = NoStaffRead()
    for path in pending_release_paths():
        try:
            intent = read_release_intent(path)
            if isinstance(intent, NoReleaseIntent):
                continue
            pending = release_notices(intent)
            notices = intent["notices"]
            if isinstance(pending, CycleNoticesToSend) and notices["kind"] == "cycle":
                if isinstance(membership, NoStaffRead):
                    try:
                        membership = staff.initial_snapshot()
                    except staff.StaffUnreadable:
                        membership = None
                unsent = recipients_text(pending.entries, notices["recipients"], membership)
                if pending.queued:
                    unsent += "; queued: " + recipients_text(pending.queued, notices["recipients"], membership)
            else:
                unsent = "every session (broadcast)"
            if isinstance(pending, CycleNoticesToSend) and pending.unresolved_stops:
                unsent += f". Pending stop confirmations: {pending.unresolved_stops}"
        except (OSError, ValueError) as error:
            if isinstance(error, ReleaseNoticeReadError):
                path = error.path
            lines.append(f"release pending: {path} could not be read: {error}; /build:hold release sets it aside")
            continue
        released = aware_instant(intent["released_at"]).astimezone(user_zone())
        lines.append(f"release pending: {intent['holder_name']}, released {released:%H:%M %Z} - unsent: {unsent}; run /build:hold release to finish it")
    return lines


def recorded_releases(held: ReleaseLockHeld) -> list[RecordedRelease]:
    return release_records_for_change(held).records


def release_records_for_change(held: ReleaseLockHeld) -> ReleaseRecordsForChange:
    """Read releases under the lock, preserving damaged pending records and continuing."""
    held.require()
    # Migrate old receipts once; later releases read only one receipt per holder.
    for path in [release_directory() / "completed-release.json",
                 *sorted((release_directory() / "releases").glob("*/completed-release.json"))]:
        intent = read_release_intent(path)
        if not isinstance(intent, NoReleaseIntent):
            complete_release_record(held, RecordedRelease(path, intent, "completed"))
    records: list[RecordedRelease] = []
    set_aside: list[DamagedRecordSetAside] = []
    for state in ("pending", "completed"):
        if state == "pending":
            paths = pending_release_paths()
        else:
            paths = sorted((release_directory() / "receipts").glob("*/completed-release.json"))
        for path in paths:
            try:
                intent = read_release_intent(path)
            except (OSError, ValueError) as error:
                if state != "pending":
                    raise
                saved = set_aside_damaged_record(held, path)
                set_aside.append(saved)
                print(f"release pending: {path} could not be read: {error}; set aside as {saved.path}", flush=True)
                continue
            if not isinstance(intent, NoReleaseIntent):
                records.append(RecordedRelease(path, intent, state))
    return ReleaseRecordsForChange(records, set_aside)


def recorded_release_holder(directory: Path, given: str | None, *, resume: bool) -> HolderIdentity | NoReleaseIntent | DamagedRecordSetAside:
    """Resolve a stopped or completed release without accepting an unrelated holder."""
    with release_lock(directory) as held:
        # A completed release must never hide a current hold.
        if given is None and resume:
            if has_holder_file(directory):
                return NoReleaseIntent()
        else:
            try:
                _ = held_key(directory, given)
            except ValueError:
                pass
            else:
                return NoReleaseIntent()
        recovered = release_records_for_change(held)
        for record in recovered.records:
            intent = record.intent
            if intent["holder_directory"] != str(directory.resolve()):
                continue
            if given is None and resume:
                return HolderIdentity(intent["holder_key"], intent["holder_name"])
            identity = holder_of(given)
            if holder_path(directory, identity.key).name == intent["holder_key"]:
                return identity
        if recovered.set_aside and not has_holder_file(directory):
            for record in recovered.records:
                if record.state == "pending" and record.intent["holder_directory"] == str(directory.resolve()):
                    return HolderIdentity(record.intent["holder_key"], record.intent["holder_name"])
            return recovered.set_aside[0]
    return NoReleaseIntent()


def finish_release_intent(held: ReleaseLockHeld, intent: ReleaseIntent, clock: Callable[[], datetime]) -> str:
    """Drain all unfinished releases for this holder directory under release.lock."""
    directory = Path(intent["holder_directory"])
    held.require()
    require_hold_write(directory)
    state = read_holders(directory)
    if isinstance(state, ActiveHolders):
        raise ValueError(resume_refusal(state.holders))
    result = "released, builds may resume."
    failures: list[str] = []
    records = [record for record in recorded_releases(held)
               if record.intent["holder_directory"] == intent["holder_directory"]]
    oldest_pending = min((aware_instant(record.intent["released_at"]) for record in records
                          if record.state == "pending"), default=None)
    if oldest_pending is None:
        return result
    newest: dict[str, str] = {}
    for record in sorted(records, key=lambda item: aware_instant(item.intent["released_at"]), reverse=True):
        if aware_instant(record.intent["released_at"]) < oldest_pending:
            break
        try:
            expired, checks = prune_pending_restarts(held, record, newest, clock)
            if expired:
                result += f" Expired {expired} restart notices."
            if record.state == "pending":
                result += finish_recorded_notices(held, record, checks)
        except Exception as error:
            # A failed record must not stop notices for another release.
            failures.append(f"{record.intent['holder_name']}: {error}")
    if failures:
        raise OSError(result + " Release notices remain pending: " + "; ".join(failures))
    return result


def release_restart_entries(notices: CycleReleaseNotices, cycle: HoldCycle | None) -> dict[str, ReleaseEntry]:
    return stopped_run_entries([*(cycle["entries"] if cycle is not None else ()), *notices.get("runs", []),
                                *(request["entry"] for request in notices.get("stop_requests", []))])


def restart_notice_max_age() -> timedelta:
    limit = conf.read("build_targets").value("restart_notice.max_age_seconds", conf.positive_whole)
    return timedelta(seconds=limit)


def prune_pending_restarts(held: ReleaseLockHeld, record: RecordedRelease, newest: dict[str, str],
                           clock: Callable[[], datetime]) -> tuple[int, RestartNoticeChecks | None]:
    """Remove expired or superseded restarts and record delivered restarts."""
    notices = record.intent["notices"]
    if notices["kind"] == "broadcast":
        return 0, None
    try:
        cycle = read_release_cycle(notices)
    except ReleaseNoticeReadError:
        cycle = None
    runs = release_restart_entries(notices, cycle)
    for entry in runs.values():
        if entry["delivery"] in ("Delivered", "QueuedUnconfirmed"):
            _ = newest.setdefault(entry["session_id"], notices["cycle_id"])
    if record.state == "completed":
        return 0, None
    pending = {run_id: entry for run_id, entry in runs.items() if release_notice_pending(entry)}
    if not pending:
        return 0, None
    try:
        checks = RestartNoticeChecks(aware_instant(record.intent["released_at"]), restart_notice_max_age(),
                                     clock, notices["cycle_id"], newest)
        expired: set[str] = set(pending) if checks.expired() else set()
        dropped = expired | {run_id for run_id, entry in pending.items()
                             if newest.get(entry["session_id"], notices["cycle_id"]) != notices["cycle_id"]}
        discard_restart_notices(held, record, cycle, dropped)
    except (OSError, conf.Unusable):
        _ = finish_recorded_notices(held, record, ordinary_only=True)
        raise
    return len(expired), checks


def discarded_stop_waits(runs: Mapping[str, ReleaseEntry], dropped: set[str], recipients: Mapping[str, str],
                         independent_waits: Mapping[str, bool], stop_waits: set[str]) -> list[str]:
    owners = {entry["session_id"].removeprefix("codex:") for run_id, entry in runs.items() if run_id in dropped}
    owners.difference_update(entry["session_id"].removeprefix("codex:")
                             for run_id, entry in runs.items() if run_id not in dropped)
    return [session for session in recipients if session.removeprefix("codex:") in owners
            and independent_waits.get(session) is not True
            and (independent_waits.get(session) is False or session in stop_waits)]


def discard_restart_notices(held: ReleaseLockHeld, record: RecordedRelease, cycle: HoldCycle | None,
                            dropped: set[str]) -> None:
    """Remove discarded notices from both release records."""
    if not dropped:
        return
    held.require(record.path)
    notices = record.intent["notices"]
    assert notices["kind"] == "cycle"
    stop_waits = set(notices.get("stop_waits", []))
    first = first_release_entries(cycle) if cycle is not None else {}
    independent_waits = cycle.get("independent_waits", {}) if cycle is not None else {}
    runs = release_restart_entries(notices, cycle)
    for session in discarded_stop_waits(runs, dropped, notices["recipients"], independent_waits, stop_waits):
        del notices["recipients"][session]
        if cycle is not None:
            _ = cycle["recipients"].pop(session, None)
            _ = independent_waits.pop(session, None)
            if (wait := first.get(session)) is not None:
                cycle["entries"].remove(wait)
    notices["runs"] = [entry for entry in notices.get("runs", [])
                       if "run" not in entry or entry["run"]["id"] not in dropped]
    notices["stop_requests"] = [request for request in notices.get("stop_requests", [])
                                if "run" not in (entry := request["entry"]) or entry["run"]["id"] not in dropped]
    if cycle is not None:
        forget_restart_notices(cycle, dropped)
    write_text_atomic(record.path, json.dumps(record.intent, ensure_ascii=False) + "\n")


def forget_restart_notices(cycle: HoldCycle, dropped: set[str]) -> None:
    cycle["entries"] = [entry for entry in cycle["entries"]
                        if "run" not in entry or entry["run"]["id"] not in dropped]
    save_cycle(cycle)


def invalid_pending_restarts(pending: CycleNoticesToSend) -> set[str]:
    """Check unsent restarts with a readable or absent confirmation."""
    confirmed = stopped_run_entries(pending.entries)
    unsent = stopped_run_entries([*pending.entries, *pending.absent_confirmations])
    delivered = {entry["run"]["id"] for entry in pending.cycle["entries"]
                 if "run" in entry and not release_notice_pending(entry)}
    return {run_id for run_id, entry in unsent.items() if run_id not in delivered
            and restart_evidence(entry, confirmed=run_id in confirmed) == "invalid"}


def finish_recorded_notices(held: ReleaseLockHeld, record: RecordedRelease, checks: RestartNoticeChecks | None = None,
                            *, ordinary_only: bool = False) -> str:
    intent = record.intent
    directory = Path(intent["holder_directory"])
    held.require()
    require_hold_write(directory)
    released_hold = {"name": intent["holder_name"], "purpose": intent.get("holder_purpose", ""),
                     "since": intent["holder_since"], "released_at": intent["released_at"]}
    released_holds = intent.get("cycle_holders", {intent["holder_key"]: released_hold})
    try:
        pending = release_notices(intent)
    except ReleaseNoticeReadError as error:
        # The intent still names its recipients. Preserve the damaged cycle and
        # rebuild only that release's delivery state; an uncertain send can repeat.
        # A stop after the rename leaves the intent, so a missing cycle also recovers.
        if error.path.exists() or error.path.is_symlink():
            saved = set_aside_damaged_record(held, error.path)
            print(f"release pending: {error.path} could not be read: {error}; set aside as {saved.path}", flush=True)
        notices = intent["notices"]
        assert notices["kind"] == "cycle"
        save_cycle({"id": notices["cycle_id"], "opened_at": intent["holder_since"],
                    "holders": released_holds,
                    "recipients": dict(notices["recipients"]), "entries": [], "release_started_at": intent["released_at"]})
        pending = release_notices(intent)
    if not isinstance(pending, CycleNoticesToSend):
        # The pending record is the broadcast entry. Keep it until the send returns.
        send_release_broadcast(release_notice(released_holds))
        result = " No hold cycle: broadcast this release to every session."
    else:
        cycle = pending.cycle
        notices = intent["notices"]
        assert notices["kind"] == "cycle"
        changed = False
        # A gone entry outside the cycle's recipients marks a quiet skip.
        first = first_release_entries(cycle)
        for session_id, name in notices["recipients"].items():
            entry = first.get(session_id)
            if (session_id not in cycle["recipients"]
                    and (entry is None or entry["delivery"] != "RecipientGone" or release_notice_pending(entry))):
                cycle["recipients"][session_id] = name
                changed = True
        for key, instants in cycle["holders"].items():
            if (holder_path(directory, key).name == intent["holder_key"] and not instants["released_at"]
                    and aware_instant(instants["since"]) == aware_instant(intent["holder_since"])):
                instants["released_at"] = intent["released_at"]
                changed = True
        if not cycle["release_started_at"]:
            cycle["release_started_at"] = intent["released_at"]
            changed = True
        if changed:
            save_cycle(cycle)
        dropped: set[str] = set()
        rejected: set[str] = set()
        expired: set[str] = set()
        if not ordinary_only:
            dropped = invalid_pending_restarts(pending)
            if dropped:
                discard_restart_notices(held, record, cycle, dropped)
                pending = release_notices(intent)
                assert isinstance(pending, CycleNoticesToSend)
                cycle = pending.cycle
            rejected, expired = send_cycle_notices(cycle, [entry for entry in pending.entries if "run" in entry], checks)
            discard_restart_notices(held, record, cycle, rejected | expired)
        _ = send_cycle_notices(cycle, [entry for entry in release_entries(cycle, notices["recipients"])
                                      if "run" not in entry and release_notice_pending(entry)])
        if ordinary_only:
            return ""
        pending = release_notices(intent)
        assert isinstance(pending, CycleNoticesToSend)
        result = not_reached_detail(pending.cycle)
        if dropped or rejected:
            result = f" Dropped {len(dropped | rejected)} invalid restart notices." + result
        if expired:
            result = f" Expired {len(expired)} restart notices." + result
        if pending.unresolved_stops:
            return result + f" Pending stop confirmations: {pending.unresolved_stops}."
        if any(release_notice_pending(entry) for entry in pending.entries):
            return result
    complete_release_record(held, record)
    return result


def open_cycle(now: datetime) -> HoldCycle:
    cycle: HoldCycle = {"id": uuid.uuid4().hex, "opened_at": now.isoformat(), "holders": {}, "recipients": {}, "entries": [], "release_started_at": ""}
    save_cycle(cycle)
    temporary = release_directory() / "current.tmp"
    _ = temporary.write_text(cycle["id"] + "\n")
    os.replace(temporary, release_directory() / "current")
    return cycle


def start_hold(directory: Path, identity: HolderIdentity, purpose: str, request: ReleaseRequest, now: datetime,
               keep: Sequence[ProcessIdentity] = (), *,
               user_words: str = "",
               before_publish: Callable[[], None] | None = None,
               after_publish: Callable[[Path], None] | None = None) -> str:
    words = require_user_words(user_words)
    with release_lock(directory) as held:
        at = datetime.now(timezone.utc)
        path = holder_path(directory, identity.key)
        new_hold = not path.is_file()
        if isinstance(read_holders(directory), NoHolders):
            cycle = open_cycle(now)
        else:
            cycle = read_cycle_for_change(held)
        notice = write_hold(directory, identity, purpose, request, now, keep, user_words=words,
                            before_publish=before_publish)
        if new_hold:
            record_hold_event("began", read_holder(path), at)
        if not isinstance(cycle, (NoCycle, DamagedRecordSetAside)):
            if cycle["release_started_at"]:
                cycle["release_started_at"] = ""
                for entry in cycle["entries"]:
                    entry["delivery"] = "NotAttempted"
            cycle["holders"][identity.key] = {"name": identity.name, "purpose": purpose,
                                              "since": now.isoformat(), "released_at": ""}
            save_cycle(cycle)
        if after_publish is not None:
            after_publish(path)
        return notice


def record_recipients(recipients: Sequence[tuple[str, str]]) -> None:
    if not has_holder_file(holder_directory()):
        return
    with release_lock() as held:
        if isinstance(read_holders(holder_directory()), NoHolders):
            return
        cycle = read_cycle_for_change(held)
        if isinstance(cycle, (NoCycle, DamagedRecordSetAside)):
            return
        independent_waits = cycle.setdefault("independent_waits", {})
        for name, session_id in recipients:
            if session_id:
                cycle["recipients"][session_id] = name
                independent_waits[session_id] = True
                for entry in cycle["entries"]:
                    if "run" not in entry and entry["session_id"] == session_id:
                        entry["name"] = name
        save_cycle(cycle)


def register_wait(session_id: str, name: str = "", *, independent: bool = True) -> str:
    if not session_id:
        return "NoSessionId: hold remains active; this session cannot register"
    if not has_holder_file(holder_directory()):
        return "no build hold"
    with release_lock():
        if isinstance(read_holders(holder_directory()), NoHolders):
            return "no build hold"
        cycle = read_cycle()
        if isinstance(cycle, NoCycle):
            return "no hold cycle: wait for the release broadcast"
        entries = first_release_entries(cycle)
        if independent:
            cycle.setdefault("independent_waits", {})[session_id] = True
        elif session_id not in cycle["recipients"] and session_id not in entries:
            _ = cycle.setdefault("independent_waits", {}).setdefault(session_id, False)
        _ = cycle["recipients"].setdefault(session_id, name or session_id)
        if session_id not in entries:
            cycle["entries"].append(new_entry(session_id, cycle["recipients"][session_id]))
        save_cycle(cycle)
        return f"registered {session_id}; wait for release before building"


def track_stop_log(held: ReleaseLockHeld, name: str) -> None:
    """Track the log even when its holder leaves before the cycle ends."""
    cycle = read_cycle_for_change(held)
    if isinstance(cycle, (NoCycle, DamagedRecordSetAside)):
        return
    logs = cycle.setdefault("stop_logs", [])
    if name not in logs:
        logs.append(name)
        save_cycle(cycle)


def save_stop_confirmation(held: ReleaseLockHeld, request_id: str, status: StopStatus) -> None:
    """Save the signal outcome before the audit update."""
    path = stop_confirmation_path(request_id)
    held.require(path)
    write_stop_confirmation(path, status)


def stop_request_status(request: StopRequest) -> StopConfirmation:
    status = read_stop_confirmation_state(request["id"])
    if status in ("stopped", "not_stopped"):
        return status
    runner_record = request.get("runner_record")
    run = request["entry"].get("run")
    if not runner_record or run is None:
        return status

    from scripts.agents import detached_runs, worker_lifecycle

    path = Path(runner_record)
    try:
        if detached_runs.confirms_hold_stop(path, run["id"], request["id"]):
            return "stopped"
        for source in (path, path.with_suffix(detached_runs.CONFIRMED_STOP_SUFFIX)):
            fields = worker_lifecycle.read_json_object(source)
            if not fields and not source.exists():
                continue
            recorded_run = fields.get("run_id")
            requests = fields.get("hold_stop_requests", [])
            if (not isinstance(recorded_run, str) or not isinstance(requests, list)
                    or any(not isinstance(item, str) for item in cast(list[object], requests))):
                return "unreadable"
            if recorded_run == run["id"] and request["id"] in requests:
                return "unreadable"
    except (OSError, ValueError):
        return "unreadable"
    # Prune can copy the confirmation before it removes the run record.
    return read_stop_confirmation_state(request["id"])


def read_restart_entry(value: object) -> ReleaseEntry:
    fields = cast(dict[str, object], value) if isinstance(value, dict) else {}
    match fields:
        case {"session_id": str(session_id), "name": str(name), "delivery": "NotAttempted", "run": run}:
            return {**new_entry(session_id, name), "run": read_stopped_run(run)}
        case _:
            raise ValueError("invalid release intent: stopped run notice")


def collect_stop_notices(cycle: HoldCycle, notices: CycleReleaseNotices) -> None:
    """Save confirmed notices and unresolved requests in the release intent."""
    from scripts.buildlog import store

    runs = stopped_run_entries(cycle["entries"])
    pending: list[StopRequest] = []
    logs = cycle.get("stop_logs", [])
    if logs:
        folder = store.root() / store.host_name()
        for name in logs:
            for row in store.locked_rows(folder / name):
                restart, status = row.get("restart"), row.get("stop_status")
                if row.get("kind") != "hold_build" or not isinstance(restart, dict):
                    continue
                request_id = row.get("stop_request_id")
                if (status == "not_stopped" and isinstance(request_id, str) and re.fullmatch(r"[0-9a-f]{32}", request_id)
                        and read_stop_confirmation_state(request_id) == "stopped"):
                    status = "stopped"
                if status not in ("stopped", "requested"):
                    continue
                entry = read_restart_entry(cast(dict[str, object], restart))
                if status == "stopped":
                    run = entry.get("run")
                    assert run is not None
                    _ = runs.setdefault(run["id"], entry)
                elif isinstance(request_id, str):
                    request: StopRequest = {"id": request_id, "entry": entry}
                    if isinstance(runner_record := row.get("runner_record"), str):
                        request["runner_record"] = runner_record
                    pending.append(request)
    if runs:
        notices["runs"] = [{**entry, "delivery": "NotAttempted"} for entry in runs.values()]
    if pending:
        notices["stop_requests"] = pending


def new_entry(session_id: str, name: str) -> ReleaseEntry:
    return {"session_id": session_id, "name": name, "delivery": "NotAttempted"}


def socket_for(session_id: str) -> RecipientLive | RecipientGone | RecipientLookupFailed:
    script = Path(__file__).resolve().parent.parent / "message" / "sessions.py"
    reason = ""
    for attempt in range(LOOKUP_TRIES):
        if attempt:
            time.sleep(LOOKUP_RETRY_S)
        try:
            result = subprocess.run([str(Path(__file__).resolve().parents[1] / "lib/py"), str(script), "socket", f"session:{session_id}"], capture_output=True, text=True)
        except OSError as error:
            reason = str(error)
            continue
        # Exit 1 is no such live session. Any other failure is the lookup itself failing.
        if result.returncode in (0, 1):
            socket = result.stdout.strip()
            return RecipientLive(socket) if result.returncode == 0 and socket else RecipientGone()
        reason = result.stderr.strip() or f"sessions.py exited {result.returncode}"
    return RecipientLookupFailed(reason)


def release_notice(holders: Mapping[str, Mapping[str, str]]) -> str:
    """Name each released hold in the user's time zone."""
    descriptions: list[str] = []
    zone = user_zone()
    today = datetime.now(zone).date()
    for key, holder in holders.items():
        name = holder.get("name", key)
        purpose = holder.get("purpose", "")
        work = f" for {purpose}" if purpose else ""
        started = aware_instant(holder["since"]).astimezone(zone)
        form = "%H:%M:%S %Z" if started.date() == today else "%m-%d %H:%M:%S %Z"
        descriptions.append(f"{name} hold{work}, from {started.strftime(form)}")
    return RELEASE_NOTICE + " Released holds: " + "; ".join(descriptions) + "."


ReleaseNoticeResult = ReleaseDelivered | ReleaseQueued | ReleaseWorkerClosed | RecipientGone | ReleaseSendFailed


def send_release(entry: ReleaseEntry, socket: str, notice: str) -> ReleaseNoticeResult:
    """Tell a recipient to start its builds; the memory gate orders them."""
    return send_release_to([entry], f"uds:{socket}", notice)


def send_release_to(entries: Sequence[ReleaseEntry], address: str, notice: str) -> ReleaseNoticeResult:
    """Send the same release text to a session socket or a Codex thread. Name each session of the entries once."""
    sessions = list(dict.fromkeys(entry["session_id"] for entry in entries))
    notice += f" Session {sessions[0]}." if len(sessions) == 1 else f" Sessions {', '.join(sessions)}."
    try:
        flag = "--exact-thread" if "run" in entries[0] else "--skip-closed"
        options = (flag,) if address.startswith("codex:") else ()
        result = subprocess.run(session_send.send_argv("--to", address, "--text", notice, *options),
                                capture_output=True, text=True)
    except OSError as error:
        return ReleaseSendFailed(str(error))
    if result.returncode == 0:
        return ReleaseDelivered()
    if result.returncode == 1:
        return ReleaseQueued()
    if options and result.returncode == SEND_CLOSED_EXIT:
        return ReleaseWorkerClosed()
    if options == ("--skip-closed",) and result.returncode == SEND_ABSENT_EXIT:
        return RecipientGone()
    return ReleaseSendFailed(f"send.py: {failure_detail(result)}")


def send_session_release(entry: ReleaseEntry, notice: str) -> ReleaseNoticeResult:
    session_id = entry["session_id"]
    if session_id.startswith("codex:"):
        return send_release_to([entry], session_id, notice)
    recipient = socket_for(session_id)
    if isinstance(recipient, RecipientLive):
        return send_release(entry, recipient.socket, notice)
    return recipient if isinstance(recipient, RecipientGone) else ReleaseSendFailed(recipient.reason)


def run_ids_text(runs: Sequence[StoppedRun]) -> str:
    """Name each run ID once. Runs with the same label share it."""
    ids: dict[str, list[str]] = {}
    for run in runs:
        ids.setdefault(run["label"], []).append(run["id"])
    return "; ".join(f"{label} (Run ID{'s' if len(group) > 1 else ''} {', '.join(group)})"
                     for label, group in ids.items())


def stopped_runs_text(entries: Sequence[ReleaseEntry], membership: staff.StaffSnapshot | None) -> str:
    """List stopped runs by owner."""
    owners: dict[str, list[StoppedRun]] = {}
    for entry in entries:
        run = entry.get("run")
        assert run is not None
        owners.setdefault(entry_name(entry, {}, membership), []).append(run)
    if len(entries) == 1:
        (name, (run,)), = owners.items()
        return f" Restart the stopped run {run['label']} for {name}. Run ID {run['id']}."
    if len(owners) == 1:
        (name, runs), = owners.items()
        return f" Restart the {len(entries)} stopped runs for {name}: {run_ids_text(runs)}."
    return f" Restart the {len(entries)} stopped runs." + "".join(
        f" For {name}: {run_ids_text(runs)}." for name, runs in owners.items())


def notice_chunks(entries: Sequence[ReleaseEntry]) -> list[list[ReleaseEntry]]:
    """Split the entries of one recipient so that each notice fits in one argv string."""
    return [list(entries[start:start + RELEASE_NOTICE_RUN_LIMIT])
            for start in range(0, len(entries), RELEASE_NOTICE_RUN_LIMIT)]


def notice_batches(entries: Sequence[ReleaseEntry]) -> list[list[ReleaseEntry]]:
    """Group stopped runs by their saved session."""
    batches: list[list[ReleaseEntry]] = []
    sessions: dict[str, list[ReleaseEntry]] = {}
    for entry in entries:
        if "run" not in entry:
            batches.append([entry])
            continue
        session = entry["session_id"]
        if session not in sessions:
            sessions[session] = []
            batches.append(sessions[session])
        sessions[session].append(entry)
    return [chunk for batch in batches for chunk in notice_chunks(batch)]


def process_restart_recipient(environment: Mapping[str, str]) -> RunAgent | NoRunAgent:
    thread = environment.get("CODEX_THREAD_ID", "")
    session = environment.get("CLAUDE_CODE_SESSION_ID", "")
    if thread:
        return RunAgent(f"codex:{thread}")
    return RunAgent(session) if session else NoRunAgent()


RestartEvidence = Literal["matches", "invalid", "unreadable"]


def staff_session_evidence(staff_id: str, session: str) -> RestartEvidence:
    """Check the saved session against the current staff records."""
    if not staff_id:
        return "matches"
    from scripts.production import staff

    try:
        membership = staff.initial_snapshot()
    except staff.StaffUnreadable:
        return "unreadable"
    saved = session.removeprefix("codex:")
    owner = membership.by_id(staff.StaffId(staff_id))
    if owner is not None and owner.current_session == saved:
        return "matches"
    return "invalid" if membership.by_current_session(saved) is not None else "unreadable"


def restart_environment(pid: int, proc_root: Path) -> dict[str, str]:
    return dict(item.decode("utf-8", "replace").split("=", 1)
                for item in (proc_root / str(pid) / "environ").read_bytes().split(b"\0") if b"=" in item)


def restart_evidence(entry: ReleaseEntry, *, confirmed: bool) -> RestartEvidence:
    """Check saved evidence against the current process and run record."""
    from scripts.agents import worker_lifecycle

    run = entry.get("run")
    assert run is not None
    session = run.get("owner_session", "")
    pid, start, boot = run.get("pid", 0), run.get("proc_start", ""), run.get("boot_id", "")
    if not pid or not start or not boot or not session or session != entry["session_id"]:
        return "invalid"
    path = run.get("record_path")
    try:
        if boot_identity(PROC_ROOT) != boot:
            return "invalid"
        if path:
            record_path = Path(path)
            record = worker_lifecycle.read_json_object(record_path)
            if record or record_path.exists():
                command = record.get("command_pid", record.get("command_group"))
                match record, command:
                    case ({"run_id": str(run_id), "status": "running" | "finished",
                           "command_started": str(command_started), "staff_id": str(staff_id)}, int(command_pid)):
                        if (run_id, command_pid, command_started, staff_id) != (run["id"], pid, start, run["staff_id"]):
                            return "invalid"
                    case _:
                        return "unreadable"
            elif not confirmed:
                return "invalid"
        current = process_start.proc_table_start(pid, PROC_ROOT)
        if isinstance(current, process_start.Started) and current.start == start:
            try:
                environment = restart_environment(pid, PROC_ROOT)
            except (FileNotFoundError, ProcessLookupError):
                current = process_start.proc_table_start(pid, PROC_ROOT)
                if isinstance(current, process_start.Started):
                    return "invalid"
            else:
                if process_restart_recipient(environment) != RunAgent(session):
                    return "invalid"
                process_staff = environment.get("BUILDLOG_OWNER_STAFF_ID", "")
                if (process_staff or not path) and process_staff != run["staff_id"]:
                    return "invalid"
                current = process_start.proc_table_start(pid, PROC_ROOT)
        if isinstance(current, process_start.Unreadable):
            return "unreadable"
        if current == process_start.Started(start) or (
                confirmed and (current == process_start.Zombie(start) or isinstance(current, process_start.Gone))):
            return "matches"
        return "invalid"
    except (OSError, ValueError):
        return "unreadable"


def ready_restart_notices(cycle: HoldCycle, batch: Sequence[ReleaseEntry], checks: RestartNoticeChecks,
                          dropped: set[str], expired: set[str]) -> list[ReleaseEntry]:
    """Check each run after address lookup and check the age before delivery."""
    ready: list[ReleaseEntry] = []
    unreadable: list[ReleaseEntry] = []
    unverified: list[ReleaseEntry] = []
    for entry in batch:
        run = entry.get("run")
        assert run is not None
        match restart_evidence(entry, confirmed=True):
            case "invalid":
                dropped.add(run["id"])
            case "unreadable":
                unreadable.append(entry)
            case "matches":
                match staff_session_evidence(run["staff_id"], entry["session_id"]):
                    case "matches":
                        ready.append(entry)
                    case "invalid":
                        dropped.add(run["id"])
                    case "unreadable":
                        unverified.append(entry)
    if unreadable:
        save_outcome(cycle, unreadable, ReleaseSendFailed("restart evidence is unreadable"))
    if unverified:
        save_outcome(cycle, unverified, ReleaseSendFailed("the saved staff session could not be verified"))
    if ready and checks.expired():
        expired.update(stopped_run_entries(ready))
        return []
    return ready


def send_checked_restarts(batch: Sequence[ReleaseEntry], address: str, notice: str,
                          membership: staff.StaffSnapshot | None, checks: RestartNoticeChecks) -> ReleaseNoticeResult:
    """Record the checked sessions and send their restart notices."""
    for entry in batch:
        _ = checks.newest.setdefault(entry["session_id"], checks.cycle_id)
    return send_release_to(batch, address, notice + stopped_runs_text(batch, membership))


def run_director(entry: ReleaseEntry, membership: staff.StaffSnapshot | None) -> staff.StaffRow | None | ReleaseSendFailed:
    """Find the director of a run whose owner is closed or gone."""
    from scripts.production import staff, worker_admission

    if membership is None:
        return ReleaseSendFailed("staff membership is unavailable")
    run = entry.get("run")
    assert run is not None
    worker = (membership.by_id(staff.StaffId(run["staff_id"])) if run["staff_id"]
              else membership.by_current_session(entry["session_id"].removeprefix("codex:")))
    director = membership.parent_of(worker) if worker is not None else None
    if director is None and run["director_dir"]:
        try:
            director = worker_admission.parent(membership, run=Path(run["director_dir"]))
        except (worker_admission.RunOwnerUnavailable, staff.StaffUnreadable) as error:
            return ReleaseSendFailed(str(error))
    return director


def send_release_broadcast(notice: str) -> None:
    script = Path(__file__).resolve().parent.parent / "production" / "broadcast.py"
    result = subprocess.run([str(Path(__file__).resolve().parents[1] / "lib/py"), str(script), "--from", "build-hold", "--all", notice],
                            capture_output=True, text=True)
    if result.returncode:
        raise OSError(f"release broadcast: {failure_detail(result)}")


def entry_text(entry: ReleaseEntry) -> str:
    return f"{entry['name']} [{entry['session_id']}]: {entry['delivery']}"


def cycle_status_lines(cycle: HoldCycle) -> list[str]:
    return [entry_text(entry) for entry in cycle["entries"]]


def not_reached_detail(cycle: HoldCycle) -> str:
    entries = release_entries(cycle, cycle["recipients"])
    # Name each session once, however many of its runs are listed.
    missing = list({entry["session_id"]: f"{entry['name']} (session gone)" for entry in entries
                    if entry["delivery"] == "RecipientGone" and not release_notice_pending(entry)}.values())
    pending: dict[str, str] = {}
    for entry in entries:
        if release_notice_pending(entry):
            _ = pending.setdefault(entry["session_id"], entry["name"])
    failed = " Not reached, tell them yourself: " + ", ".join(missing) if missing else ""
    if not pending:
        return failed
    return failed + ("." if failed else "") + " Release notices still pending for " + ", ".join(pending.values()) + "."


def notify_release(cycle: HoldCycle) -> str:
    with release_lock():
        current = read_cycle()
        if (isinstance(current, NoCycle) or current["id"] != cycle["id"]
                or not current["release_started_at"]
                or current["release_started_at"] != cycle["release_started_at"]):
            return ""
        return notify_released_cycle(current)


def notify_released_cycle(cycle: HoldCycle) -> str:
    if has_holder_file(holder_directory()):
        return ""
    runs = stopped_run_entries(cycle["entries"])
    pending = {run_id for run_id, entry in runs.items() if release_notice_pending(entry)}
    checks = (RestartNoticeChecks(aware_instant(cycle["release_started_at"]), restart_notice_max_age(),
                                  lambda: datetime.now(timezone.utc), cycle["id"], {}) if pending else None)
    expired: set[str] = pending if checks is not None and checks.expired() else set()
    dropped, expired_during_send = send_cycle_notices(
        cycle, [entry for run_id, entry in runs.items()
                if entry["delivery"] == "NotAttempted" and run_id not in expired], checks)
    expired |= expired_during_send
    if expired:
        independent_waits = cycle.get("independent_waits", {})
        first = first_release_entries(cycle)
        for session in discarded_stop_waits(runs, expired, cycle["recipients"], independent_waits, set()):
            del cycle["recipients"][session]
            _ = independent_waits.pop(session, None)
            if (wait := first.get(session)) is not None:
                cycle["entries"].remove(wait)
    if expired or dropped:
        forget_restart_notices(cycle, expired | dropped)
    _ = send_cycle_notices(cycle, [entry for entry in release_entries(cycle, cycle["recipients"])
                                  if "run" not in entry and entry["delivery"] == "NotAttempted"])
    detail = f" Expired {len(expired)} restart notices." if expired else ""
    if dropped:
        detail += f" Dropped {len(dropped)} invalid restart notices."
    return detail + not_reached_detail(cycle)


def send_cycle_notices(cycle: HoldCycle, entries: Sequence[ReleaseEntry],
                       checks: RestartNoticeChecks | None = None) -> tuple[set[str], set[str]]:
    from scripts.production import staff

    # The release lock prevents a same-session re-hold between this check and a send.
    dropped: set[str] = set()
    expired: set[str] = set()
    if not entries or has_holder_file(holder_directory()):
        return dropped, expired

    without_staff: set[str] = set()
    try:
        membership = staff.initial_snapshot()
    except staff.StaffUnreadable:
        # An unreadable snapshot cannot prove that a staff row is absent.
        membership = None
    else:
        without_staff = {entry["session_id"] for entry in entries
                         if membership.by_member_reference(entry["session_id"].removeprefix("codex:")) is None}
    notice = release_notice(cycle["holders"])
    fallbacks: dict[str, list[ReleaseEntry]] = {}
    for batch in notice_batches(entries):
        if has_holder_file(holder_directory()):
            return dropped, expired
        save_attempt(cycle, batch)
        first = batch[0]
        if "run" not in first:
            save_outcome(cycle, batch, send_session_release(first, notice), without_staff)
            continue
        assert checks is not None
        session = first["session_id"]
        recipient = None if session.startswith("codex:") else socket_for(session)
        ready = ready_restart_notices(cycle, batch, checks, dropped, expired)
        if not ready:
            continue
        if isinstance(recipient, RecipientLookupFailed):
            result = ReleaseSendFailed(recipient.reason)
        elif isinstance(recipient, RecipientGone):
            result = recipient
        else:
            address = session if recipient is None else f"uds:{recipient.socket}"
            result = send_checked_restarts(ready, address, notice, membership, checks)
        if isinstance(result, (ReleaseWorkerClosed, RecipientGone)):
            queue_director_notices(cycle, ready, membership, result, fallbacks)
        else:
            save_outcome(cycle, ready, result)
    for address, group in fallbacks.items():
        assert checks is not None
        for chunk in notice_chunks(group):
            if has_holder_file(holder_directory()):
                return dropped, expired
            ready = ready_restart_notices(cycle, chunk, checks, dropped, expired)
            if ready:
                result = send_checked_restarts(ready, address, notice, membership, checks)
                save_outcome(cycle, ready, result)
    return dropped, expired


def queue_director_notices(cycle: HoldCycle, batch: Sequence[ReleaseEntry], membership: staff.StaffSnapshot | None,
                           owner_result: ReleaseWorkerClosed | RecipientGone,
                           fallbacks: dict[str, list[ReleaseEntry]]) -> None:
    """Group runs by director and save outcomes for runs without a route."""
    unresolved: dict[ReleaseWorkerClosed | RecipientGone | ReleaseSendFailed, list[ReleaseEntry]] = {}
    for entry in batch:
        director = run_director(entry, membership)
        if director is None or isinstance(director, ReleaseSendFailed):
            result = director if isinstance(director, ReleaseSendFailed) else owner_result
            unresolved.setdefault(result, []).append(entry)
        else:
            # Keep the attempt pending until the director send returns.
            fallbacks.setdefault(str(director.id), []).append(entry)
    for result, entries in unresolved.items():
        save_outcome(cycle, entries, result)


def save_attempt(cycle: HoldCycle, batch: Sequence[ReleaseEntry]) -> None:
    """Save the pending entries before a send. A stop before the outcome is saved may cause a harmless second notice."""
    for entry in batch:
        if entry not in cycle["entries"]:
            cycle["entries"].append(entry)
        entry["delivery"] = "NotAttempted"
    save_cycle(cycle)


def save_outcome(cycle: HoldCycle, batch: Sequence[ReleaseEntry], result: ReleaseNoticeResult,
                 without_staff: Collection[str] = ()) -> None:
    """Save one send outcome on every entry of the batch, only after the send returns."""
    session_id = batch[0]["session_id"]
    run = "run" in batch[0]
    delivery = release_delivery(result, run=run)
    for entry in batch:
        entry["delivery"] = delivery
        if (isinstance(result, RecipientGone) and not release_notice_pending(entry)
                and entry["session_id"] in without_staff):
            _ = cycle["recipients"].pop(entry["session_id"], None)
    if isinstance(result, ReleaseSendFailed) and (run or session_id.startswith("codex:")):
        print(f"build_hold: release notice for {session_id}: {result.reason}", file=sys.stderr)
    save_cycle(cycle)


def release_delivery(result: ReleaseNoticeResult, *, run: bool) -> DeliveryOutcome:
    match result:
        case ReleaseDelivered():
            return "Delivered"
        case ReleaseQueued():
            return "QueuedUnconfirmed"
        case ReleaseWorkerClosed():
            return "RecipientGone" if run else "WorkerClosed"
        case RecipientGone():
            return "RecipientGone"
        case ReleaseSendFailed():
            return "DeliveryFailed"


def release_cycle(directory: Path, name: str, *, resume: bool = False,
                  clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> str:
    with release_lock(directory) as held:
        path = holder_path(directory, name)
        if not path.is_file():
            for record in recorded_releases(held):
                intent = record.intent
                if (intent["holder_directory"] != str(directory.resolve())
                        or intent["holder_key"] != path.name):
                    continue
                if record.state == "completed":
                    if not has_holder_file(directory):
                        detail = finish_release_intent(held, intent, clock).removeprefix("released, builds may resume.")
                        return "hold is already released." + detail
                    return "hold is already released."
                return finish_release_intent(held, intent, clock)
            raise ValueError(f"{name} held nothing")
        cycle = read_cycle_for_change(held)
        if resume and not isinstance(cycle, (NoCycle, DamagedRecordSetAside)):
            holds = live_holds(cycle, directory)
            if holds:
                raise ValueError(resume_refusal(holds))
        holder = read_holder(path)
        state = read_holders(directory)
        intent: ReleaseIntent | NoReleaseIntent = NoReleaseIntent()
        if isinstance(state, ActiveHolders) and len(state.holders) == 1:
            notices: CycleReleaseNotices | BroadcastReleaseNotice = {"kind": "broadcast"}
            if not isinstance(cycle, (NoCycle, DamagedRecordSetAside)):
                notices = {"kind": "cycle", "cycle_id": cycle["id"], "recipients": dict(cycle["recipients"])}
                collect_stop_notices(cycle, notices)
                stop_waits = [session for session, independent in cycle.get("independent_waits", {}).items()
                              if not independent]
                if stop_waits:
                    notices["stop_waits"] = stop_waits
            intent = {"holder_key": holder.key, "holder_name": holder.name, "holder_purpose": holder.purpose,
                      "holder_since": holder.since.isoformat(), "holder_directory": str(directory.resolve()),
                      "released_at": clock().isoformat(), "notices": notices}
            if not isinstance(cycle, (NoCycle, DamagedRecordSetAside)):
                intent["cycle_holders"] = {key: dict(fields) for key, fields in cycle["holders"].items()}
            save_release_intent(intent)
        result = release_hold(directory, holder)
        if not isinstance(cycle, (NoCycle, DamagedRecordSetAside)):
            for key, instants in cycle["holders"].items():
                if holder_path(directory, key).name == holder.key and aware_instant(instants["since"]) == holder.since:
                    instants["released_at"] = clock().isoformat()
            save_cycle(cycle)
        if isinstance(cycle, (NoCycle, DamagedRecordSetAside)):
            if result != "released, builds may resume.":
                return result
            if isinstance(intent, NoReleaseIntent):
                raise ValueError("last holder has no release intent")
            return finish_release_intent(held, intent, clock)
        remaining = live_holds(cycle, directory)
        if remaining:
            now = clock()
            return "released; still held by " + "; ".join(holder_text(other, now) for other in remaining)
        cycle["release_started_at"] = clock().isoformat()
        save_cycle(cycle)
        if isinstance(intent, NoReleaseIntent):
            raise ValueError("last holder has no release intent")
        return finish_release_intent(held, intent, clock)


def quiet_verdict(processes: Sequence[Process], user: str) -> Quiet | Busy:
    builds = [process for process in processes if process.command in BUILD_COMMANDS]
    reasons: list[str] = []
    for owner in sorted({process.user for process in builds}, key=lambda owner: (owner != user, owner)):
        counts = Counter(process.command for process in builds if process.user == owner)
        label = "your builds still running" if owner == user else owner
        reasons.append(label + ": " + ", ".join(
            f"{count} {command}" for command, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))))
    if reasons:
        return Busy(tuple(reasons))
    return Quiet(tuple(processes))


def read_processes() -> list[Process]:
    output = subprocess.check_output(["ps", "-eo", "pid=,user:32=,comm="], text=True)
    processes: list[Process] = []
    for line in output.splitlines():
        parts = line.split(maxsplit=2)
        if len(parts) == 3:
            pid, user, command = parts
            try:
                processes.append(Process(int(pid), user, Path(command).name))
            except ValueError:
                continue
    return processes


def read_process_cpu_times(processes: Sequence[Process], proc_root: Path) -> dict[int, ProcessCpuTime]:
    readings: dict[int, ProcessCpuTime] = {}
    for process in processes:
        try:
            # comm can contain spaces and closing parentheses; the remaining fields start at 3.
            fields = (proc_root / str(process.pid) / "stat").read_text().rsplit(")", 1)[1].split()
            readings[process.pid] = ProcessCpuTime(int(fields[11]) + int(fields[12]), int(fields[19]))
        except (OSError, ValueError, IndexError):
            continue
    return readings


def heaviest_processes(
    processes: Sequence[Process],
    *,
    proc_root: Path = PROC_ROOT,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[CurrentProcessCpu, ...]:
    if not processes:
        return ()
    ticks_per_second = os.sysconf("SC_CLK_TCK")
    started = clock()
    before = read_process_cpu_times(processes, proc_root)
    sleep(1)
    after = read_process_cpu_times(processes, proc_root)
    elapsed = clock() - started
    if elapsed <= 0:
        return ()
    usage: list[CurrentProcessCpu] = []
    for process in processes:
        if process.pid not in before or process.pid not in after:
            continue
        first, last = before[process.pid], after[process.pid]
        if first.started_at_ticks != last.started_at_ticks or last.ticks <= first.ticks:
            continue
        percent = 100 * (last.ticks - first.ticks) / (elapsed * ticks_per_second)
        usage.append(CurrentProcessCpu(process, percent))
    return tuple(sorted(usage, key=lambda reading: reading.cpu_percent, reverse=True)[:5])


def wait_for_quiet(
    max_wait: float = 600,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    processes: Callable[[], Sequence[Process]] = read_processes,
    user: str | None = None,
) -> Quiet | Busy:
    if max_wait < 0:
        raise ValueError("max wait must be non-negative")
    current_user = user or os.environ.get("USER", "")
    deadline = clock() + max_wait
    while True:
        result = quiet_verdict(processes(), current_user)
        if isinstance(result, Quiet) or clock() >= deadline:
            return result
        sleep(min(10, max(0, deadline - clock())))


def main(arguments: list[str]) -> int:
    override_guard.refuse_overrides(Path(__file__), "build_hold/build_hold.py")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    clear = commands.add_parser("clear")
    _ = clear.add_argument("--holder")
    _ = clear.add_argument("--for", dest="purpose", required=True)
    _ = clear.add_argument("--user-words", required=True, help="Nate's exact words requesting the hold")
    _ = clear.add_argument("--release-eta")
    _ = clear.add_argument("--zone")
    _ = clear.add_argument("target", nargs="?")
    _ = clear.add_argument("--proc-root", type=Path, default=PROC_ROOT)
    _ = clear.add_argument("--keep", type=int, action="append", default=[])
    drain = commands.add_parser("drain")
    _ = drain.add_argument("--holder")
    _ = drain.add_argument("--for", dest="purpose", required=True)
    _ = drain.add_argument("--user-words", required=True, help="Nate's exact words requesting the hold")
    _ = drain.add_argument("--release-eta")
    _ = drain.add_argument("--zone")
    _ = drain.add_argument("target", nargs="?")
    _ = drain.add_argument("--proc-root", type=Path, default=PROC_ROOT)
    _ = drain.add_argument("--keep", type=int, action="append", default=[])
    quiet = commands.add_parser("quiet")
    _ = quiet.add_argument("--max-wait", type=float, default=600)
    release = commands.add_parser("release")
    _ = release.add_argument("--holder")
    _ = release.add_argument("--resume", action="store_true")
    _ = release.add_argument("target", nargs="?")
    _ = commands.add_parser("wait")
    recipient = commands.add_parser("record-recipient")
    _ = recipient.add_argument("--session-id", required=True)
    _ = recipient.add_argument("--name", required=True)
    for action in ("status", "audit"):
        audit = commands.add_parser(action)
        _ = audit.add_argument("--proc-root", type=Path, default=PROC_ROOT)
    options = parser.parse_args(arguments)
    try:
        action = cast(str, options.action)
        user_words = ""
        if action in {"clear", "drain"}:
            try:
                user_words = require_user_words(cast(str, options.user_words))
            except ValueError as error:
                commands.choices[action].error(str(error))
        if action in {"clear", "drain", "release"}:
            try:
                target_name = command_target_name(cast(str | None, options.target))
            except (ConfigError, OSError, UnicodeError) as error:
                print(f"target configuration cannot be read: {error}")
                return 2
            if target_name != "natedev":
                print(f"{action} {target_name} is not supported yet")
                return 2
            target = parse_hold_target(target_name)
            if isinstance(target, UnknownHoldTarget):
                print(f"unknown target {target.name}; configured targets: {', '.join(target.configured)}")
                return 2
            if isinstance(target, UncheckedHoldTarget):
                print(f"unknown target {target.name}; the target config cannot be read: {target.reason}")
                return 2
            if isinstance(target, (MacHoldTarget, ConfiguredSshHoldTarget)):
                target_name = "mac" if isinstance(target, MacHoldTarget) else target.name
                if action == "drain":
                    print(f"drain is not supported with --target {target_name}")
                    return 2
                refused: list[str] = []
                if action == "clear":
                    if cast(list[int], options.keep):
                        refused.append("--keep")
                    if cast(object, options.release_eta) is not None:
                        refused.append("--release-eta")
                    if cast(object, options.zone) is not None:
                        refused.append("--zone")
                elif cast(bool, options.resume):
                    refused.append("--resume")
                if refused:
                    print(f"{refused[0]} is not supported with --target {target_name}")
                    return 2
                identity = holder_of(cast(str | None, options.holder))
                if action == "clear":
                    command = ["block", "--holder", identity.key, "--for", cast(str, options.purpose)]
                    result = run_mac_hold(command, target_name)
                else:
                    result = release_mac_hold(identity, target_name)
                return print_mac_hold_result(result)
        if action in {"clear", "drain"} and cast(str | None, options.release_eta) is None:
            print("build_hold: give --release-eta HH:MM --zone <IANA zone> for a natedev hold")
            return 2
        if action == "drain":
            request = release_request(cast(str | None, options.release_eta), cast(str | None, options.zone))
            identity = holder_of(cast(str | None, options.holder))
            proc_root = cast(Path, options.proc_root)
            kept: list[ProcessIdentity] = []
            for pid in cast(list[int], options.keep):
                start = process_start_time(pid, proc_root)
                if not start:
                    raise ValueError(f"--keep {pid}: no live process with a start time")
                kept.append(ProcessIdentity(pid, start))
            kept.extend(hold_enforcer().drain_identities(proc_root, identity))
            purpose = cast(str, options.purpose)
            now = datetime.now(timezone.utc)
            publication_bound = 0

            def record_publication_bound() -> None:
                nonlocal publication_bound
                publication_bound = time.clock_gettime_ns(time.CLOCK_BOOTTIME) * os.sysconf("SC_CLK_TCK") // 1_000_000_000

            directory = holder_directory()
            path = holder_path(directory, identity.key)
            with ExitStack() as files:
                published: list[TextIO] = []

                def keep_published_file(record: Path) -> None:
                    # Keep the inode from reuse until the second scan ends.
                    published.append(files.enter_context(record.open()))

                _ = start_hold(directory, identity, purpose, request, now, kept, user_words=user_words,
                               before_publish=record_publication_bound, after_publish=keep_published_file)
                kept = [run for run in hold_enforcer().drain_identities(proc_root, identity)
                        if int(run.start_time) < publication_bound]
                with release_lock(directory):
                    try:
                        current = path.stat()
                    except FileNotFoundError:
                        print("The hold was released. The drain ends.", flush=True)
                        return 0
                    if not os.path.samestat(os.fstat(published[0].fileno()), current):
                        print("The holder has a newer hold. The drain ends.", flush=True)
                        return 0
                    _ = write_hold(directory, identity, purpose, request, now, kept, user_words=user_words)
                    holder = read_holder(path)
            print(f"natedev is draining for {purpose}. Existing runs finish; new natedev work waits until release.",
                  flush=True)
            hold_enforcer().start()
            return hold_enforcer().wait_for_kept_runs(holder, proc_root)
        elif action == "clear":
            request = release_request(cast(str | None, options.release_eta), cast(str | None, options.zone))
            identity = holder_of(cast(str | None, options.holder))
            keep: list[ProcessIdentity] = []
            proc_root = cast(Path, options.proc_root)
            for pid in cast(list[int], options.keep):
                start = process_start_time(pid, proc_root)
                if not start:
                    raise ValueError(f"--keep {pid}: no live process with a start time")
                keep.append(ProcessIdentity(pid, start))
            now = datetime.now(timezone.utc)

            def record_clear_cutoff() -> None:
                hold_enforcer().record_clear_cutoff(identity, now)

            print(start_hold(holder_directory(), identity, cast(str, options.purpose), request,
                             now, keep, user_words=user_words, before_publish=record_clear_cutoff))
            hold_enforcer().start()
            print_hold_audit(holder_directory(), proc_root, beginning=True)
            hold_enforcer().clear_queued_runs(proc_root)
        elif action == "quiet":
            verdict = wait_for_quiet(cast(float, options.max_wait))
            if isinstance(verdict, Busy):
                print("; ".join(verdict.reasons))
                return 1
            heaviest = heaviest_processes(verdict.processes)
            running = ", ".join(f"{reading.process.user} {reading.process.command} {reading.cpu_percent:g}%" for reading in heaviest)
            print(f"builds are quiet: no build runs. 1-minute load {os.getloadavg()[0]:g} is other work: {running or 'none visible'}")
        elif action == "release":
            given = cast(str | None, options.holder)
            resume_requested = cast(bool, options.resume)
            name: str | None = None
            recorded = recorded_release_holder(holder_directory(), given, resume=resume_requested)
            if isinstance(recorded, DamagedRecordSetAside):
                print("released, builds may resume.")
                return 0
            if isinstance(recorded, HolderIdentity) and not holder_path(holder_directory(), recorded.key).is_file():
                name = recorded.key
            if name is None and given is None and resume_requested:
                with release_lock() as held:
                    cycle = read_cycle_for_change(held)
                    if not isinstance(cycle, (NoCycle, DamagedRecordSetAside)):
                        name = next((key for key, instants in cycle["holders"].items()
                                     if instants["released_at"] and holder_path(holder_directory(), key).is_file()
                                     and aware_instant(instants["since"]) == read_holder(holder_path(holder_directory(), key)).since), None)
                        if name is None:
                            holds = live_holds(cycle, holder_directory())
                            if holds:
                                raise ValueError(resume_refusal(holds))
                    elif isinstance(cycle, DamagedRecordSetAside):
                        state = read_holders(holder_directory())
                        if isinstance(state, ActiveHolders) and len(state.holders) == 1:
                            name = state.holders[0].key
            if name is None and given is None and resume_requested:
                raise ValueError("release needs --holder, or --resume for an active release")
            if name is None:
                name = held_key(holder_directory(), given)
            print(release_cycle(holder_directory(), name, resume=resume_requested))
        elif action == "wait":
            # Use the local gate's hold and caller check; recording a notice recipient permits no build.
            import importlib
            check_local_hold = cast(Callable[[], int], getattr(
                importlib.import_module('scripts.build_targets.build_targets'), "command_check_local"))
            return check_local_hold()
        elif action == "record-recipient":
            record_recipients([(cast(str, options.name), cast(str, options.session_id))])
        elif action == "audit":
            print("\n".join(audit_lines(audit_hold(holder_directory(), cast(Path, options.proc_root)))))
        else:
            now = datetime.now(timezone.utc)
            state = read_holders(holder_directory())
            if isinstance(state, NoHolders):
                print("no build hold")
            else:
                for holder in state.holders:
                    overdue = ", overdue" if release_overdue(holder.release, now) else ""
                    since = holder.since.astimezone(user_zone()).strftime("%H:%M %Z")
                    eta = release_eta_text(holder.release)
                    state_text = holder_state(holder, cast(Path, options.proc_root))
                    print(f"{holder.name} since {since}, for {holder.display_reason()} - release eta: {eta}{overdue} - {state_text}")
                    for run in live_kept_runs(holder, cast(Path, options.proc_root)):
                        print(f"  kept by {holder.name}: pid {run.pid}, start {run.start_time}")
                for line in ci_placement_lines(cast(Path, options.proc_root)):
                    print(line)
                try:
                    cycle = read_cycle()
                except ReleaseRecordReadError as error:
                    print(release_record_error_line(error))
                else:
                    if not isinstance(cycle, NoCycle):
                        for line in cycle_status_lines(cycle):
                            print(line)
                print_hold_audit(holder_directory(), cast(Path, options.proc_root))
            for line in pending_release_status_lines():
                print(line)
    except override_guard.SharedStateWriteRefused as error:
        print(f"build_hold: {error}", file=sys.stderr)
        return 2
    except (ValueError, OSError) as error:
        print(f"build_hold: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    # The enforcer and this CLI must use the same domain types.
    _ = sys.modules.setdefault("scripts.build_hold.build_hold", sys.modules[__name__])
    sys.exit(main(sys.argv[1:]))

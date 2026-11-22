"""Stop live staff rows, then resume their current sessions in their saved tabs."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import errno
import io
import json
import math
import os
import shlex
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Generator, Iterable, Mapping
from contextlib import ExitStack, contextmanager, nullcontext, redirect_stderr, redirect_stdout, suppress
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextvars import ContextVar, copy_context
from threading import Event
from dataclasses import dataclass, field, replace
from enum import Enum
from functools import partial
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, TypedDict, TypeVar, cast
from importlib import import_module
from uuid import uuid4

from scripts.herdr import herdr
from scripts.herdr import herdr_types
from scripts.message import sessions, codex_daemon, session_records
from scripts.production import staff, session_staff, worker_admission
from scripts.accounts import accounts
from scripts.lib import conf, models_registry, nix_env, operation_deadline, unit_runs, resume_message
from scripts.lib.atomic_file import create_text_atomic
from scripts.lib.transcripts import codex_transcript_homes, open_transcript, transcript_paths
from scripts.agents import member_account, shutdown_record, pane_record
from scripts.agents.stop_results import StopResult, Stopped, NotRunning, StopFailed, shutdown_state_root
from scripts.agents.codex_trust import trust_arguments
from scripts.herdr import pane_launch
from scripts.whoami.account import UnknownAccountName, named_account_environment

if TYPE_CHECKING:
    from scripts.herdr.worker_panes import AllWorkers, SelectedWorkerRows as WorkerRowSelection
    from scripts.production.worker import RestoreReport

class StopReadArguments(TypedDict, total=False):
    """Forward shared observations only when the caller supplies them."""

    codex: herdr.CodexConversations | None


@dataclass(frozen=True)
class AllRestartAccounts:
    """Select rows without an account filter."""


ALL_RESTART_ACCOUNTS = AllRestartAccounts()
NO_RESTART_MESSAGE = herdr.NoOpeningPrompt()
DEFAULT_RESTART_CAUSE = "agents restart ran"


@dataclass(frozen=True)
class BulkCommand:
    verb: Literal["restart", "stop", "held-sessions"] = "restart"
    account: str | None = None
    all_rows: bool = False
    only: tuple[str, ...] = ()
    why: str = DEFAULT_RESTART_CAUSE
    dry_run: bool = False
    here: bool = False

    def arguments(self) -> list[str]:
        arguments: list[str] = [self.verb]
        if self.all_rows:
            arguments.append("--all")
        if self.only:
            only = ",".join(self.only)
            arguments.extend([f"--only={only}"] if only.startswith("-") else ["--only", only])
        if self.verb == "restart" and self.why != DEFAULT_RESTART_CAUSE:
            arguments.extend([f"--why={self.why}"] if self.why.startswith("-") else ["--why", self.why])
        if self.dry_run:
            arguments.append("--dry-run")
        if self.here:
            arguments.append("--here")
        if self.account:
            if self.verb == "stop":
                arguments.append("--account")
            arguments.append(self.account)
        return arguments

    def retry(self) -> str:
        return shlex.join(["agents", *self.arguments()])


def account_matches(kind: Literal["claude", "codex"], environment: dict[str, str],
                    account: accounts.AccountSelection) -> bool:
    if isinstance(account, accounts.NamedAccount):
        return kind == "claude" and Path(environment.get("CLAUDE_CONFIG_DIR") or
            accounts.main_config_dir()).expanduser().resolve() == account.dir.resolve()
    return kind == "codex" and Path(environment.get("CODEX_HOME") or
        accounts.accounts_home() / ".codex").expanduser().resolve() == account.home.resolve()


class AllRestartRows(Enum):
    CURRENT = "all staff rows"


@dataclass(frozen=True)
class SelectedRestartRows:
    """Observe only the requested fixed staff rows."""

    ids: frozenset[staff.StaffId]


class WorkerRestartScope(Enum):
    FOLLOW_OWNER = "restore the owner's workers"
    SELECTED_ROWS = "restart only selected rows"


class LiveWorkerRecovery(Enum):
    LEAVE_RUNNING = "leave a live worker unchanged"
    RESTORE_BEFORE_REMOVAL = "restore the worker before removing its restart entry"


class RestartDisposition(Enum):
    STARTED = "this attempt launched the row"
    RESTORED = "the row needs no new launch"
    HELD = "the row keeps its pause or exit record"
    REMOVED = "the row no longer exists, so its id left the restart record"


class RestartSuccessReport(Enum):
    START_ROW = "start_row prints the success line"
    CALLER = "the caller prints the success line"


@dataclass(frozen=True)
class FailedHolderStart:
    """A failed start retains its arguments for one stopped-director retry."""

    holder: staff.StaffRow
    focus: bool
    error: Exception


class ResumeCheckState(Enum):
    NO_MESSAGE = "no continuation message"
    NO_WORK = "no work after the continuation message"
    UNREADABLE = "transcript unreadable"
    RESUMED = "resumed"


@dataclass
class CodexResumeTranscript:
    """Read new turns in the account and thread used at launch."""

    transcript: herdr.SessionTranscript
    since: datetime
    offsets: dict[Path, int] = field(default_factory=dict)

    def skip_existing_turns(self) -> None:
        try:
            source = worker_admission.codex_transcript_source(self.transcript.session_id, self.transcript.config_dir)
            with open_transcript(source) as stream:
                self.offsets[source] = herdr.complete_transcript_offset(stream)
        except (OSError, ValueError):
            pass

    def progress(self, text: str) -> ResumeCheckState:
        state = ResumeCheckState.NO_MESSAGE
        try:
            source = worker_admission.codex_transcript_source(self.transcript.session_id, self.transcript.config_dir)
            offset = self.offsets.get(source, 0)
            if source.stat().st_size < offset:
                offset = 0
            for row in worker_admission.transcript_records(
                    source, offset=offset, boundary=worker_admission.TranscriptRecordBoundary.COMPLETE_LINES):
                if isinstance(row, worker_admission.TornTranscriptRecord):
                    return ResumeCheckState.UNREADABLE
                stamp = row.get("timestamp")
                if not isinstance(stamp, str):
                    continue
                at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                if at.tzinfo is None or at <= self.since:
                    continue
                entry = worker_admission.transcript_entry(row, set())
                if entry.startswith("user:\n") and text in entry:
                    state = ResumeCheckState.NO_WORK
                elif state is ResumeCheckState.NO_WORK:
                    raw = row.get("payload")
                    if isinstance(raw, dict):
                        payload = cast(dict[str, object], raw)
                        if (payload.get("role") == "assistant"
                                or payload.get("type") in ("agent_message", "function_call", "custom_tool_call")):
                            return ResumeCheckState.RESUMED
            return state
        except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
            return ResumeCheckState.UNREADABLE


@dataclass
class DirectorResumeCheck:
    name: str
    text: str
    reader: herdr.TranscriptCursor | CodexResumeTranscript

    def progress(self) -> ResumeCheckState:
        if isinstance(self.reader, CodexResumeTranscript):
            return self.reader.progress(self.text)
        turns = self.reader.read_turns()
        if isinstance(self.reader.readability, herdr.TranscriptUnreadable) or not self.reader.current_paths:
            return ResumeCheckState.UNREADABLE
        state = ResumeCheckState.NO_MESSAGE
        for turn in sorted(turns, key=lambda turn: turn[0]):
            row = turn[1]
            if state is ResumeCheckState.NO_WORK and row.get("type") == "assistant":
                return ResumeCheckState.RESUMED
            if (row.get("type") == "user" and not row.get("isMeta", False)
                    and self.text in worker_admission.transcript_entry(row, set())):
                state = ResumeCheckState.NO_WORK
        return state


class NoDirectorResumeChecks(Enum):
    CURRENT = "this caller does not check director progress"


class RetainedPaneState(Enum):
    SHELL = "the retained pane has no native agent"
    EXITING = "the retained pane still has work or unknown evidence"
    ABSENT = "the retained pane is absent"
    FOREIGN = "another terminal or native owner holds the pane"


@dataclass(frozen=True)
class RetainedRestartPane:
    """The original terminal and publication owned by the restarted row."""

    row: staff.StaffRow
    publication: pane_launch.LaunchPublication
    client: herdr.Herdr
    terminal: str

    def state(self) -> RetainedPaneState:
        try:
            pane = next((pane for pane in self.client.panes()
                         if pane["pane_id"] == self.publication.entry["pane_id"]), None)
            if pane is None:
                return RetainedPaneState.ABSENT
            if not self.terminal or pane.get("terminal_id") != self.terminal:
                return RetainedPaneState.FOREIGN
            identities = pane_launch.native_sessions(self.client, pane)
            if identities - {str(self.row.current_session)}:
                return RetainedPaneState.FOREIGN
            if not identities and isinstance(self.client.pane_activity(pane["pane_id"]), herdr.PaneAtShell):
                return RetainedPaneState.SHELL
        except (OSError, ValueError, herdr_types.HerdrFailure):
            pass
        return RetainedPaneState.EXITING


@dataclass(frozen=True)
class ReplacedRestartPane:
    """A retained source whose replacement started before its terminal became idle."""

    source: RetainedRestartPane
    destination_client: herdr.Herdr
    destination: Path
    attempt: pane_launch.ResumeAttempt
    proof: pane_launch.VerifiedResume

    def finish(self) -> bool:
        state = self.source.state()
        if state is RetainedPaneState.FOREIGN:
            return True
        if state is RetainedPaneState.EXITING:
            return False
        try:
            pane_launch.finish_restore(self.source.publication.path, self.source.publication.entry,
                self.attempt, self.proof, close=self.destination_client.close_tab_confirmed,
                destination=self.destination,
                close_source=lambda pane: self.source.client.close_tab_confirmed(pane, terminal=self.source.terminal),
                source_pane=pane_launch.ResumeSourcePane(self.source.publication.entry["pane_id"]))
            return pane_launch.read_strict(self.source.publication.path).get(
                self.source.publication.entry["pane_id"]) != self.source.publication.entry
        except (OSError, ValueError, herdr_types.HerdrFailure):
            return False


class WorkerRestoreIncomplete(OSError):
    """Keep the relevant rows in the restart record."""


@dataclass(frozen=True)
class FollowUp:
    name: str
    reason: str
    step: str
    unit: str = ""
    operator: bool = False
    director_check: bool = False

    def line(self) -> str:
        reason = " ".join(self.reason.split()).rstrip(".")
        return f"{self.name}: {reason}. {self.step}".rstrip()


class FollowUpFields(TypedDict):
    name: str
    reason: str
    step: str
    unit: str
    operator: bool
    director_check: bool


class DetachedReport(TypedDict):
    retry: str
    targets: dict[str, str]
    units: dict[str, str]
    issues: dict[str, FollowUpFields]


class LogWriter(Enum):
    LOCAL = "this process owns the report"
    CHILD = "the child claimed the report"
    UNKNOWN = "this process could not check the claim"


@dataclass
class BulkReport:
    log: Path
    command: str
    retry: str
    targets: dict[str, str] = field(default_factory=dict)
    units: dict[str, str] = field(default_factory=dict)
    completed: set[str] = field(default_factory=set)
    issues: dict[str, FollowUp] = field(default_factory=dict)
    preview: list[str] = field(default_factory=list)
    inherited: set[str] = field(default_factory=set)
    fixed_targets: frozenset[str] | None = None
    writer: LogWriter = LogWriter.LOCAL
    log_stream: io.FileIO | None = None
    log_error: OSError | ValueError | None = None
    stop_plan_recorded: bool = False
    stop_signal: signal.Signals | None = None
    not_started: dict[str, str] = field(default_factory=dict)
    reboot: bool = False
    on_finish: Callable[[str, int], None] | None = None

    def track(self, row: staff.StaffRow, record: staff.StaffSnapshot) -> None:
        owner = record.parent_of(row) if row.kind == "worker" else row
        self.targets[row.id] = f"{owner.name} / {row.name}" if row.kind == "worker" and owner is not None else row.name
        if owner is not None and owner.kind in ("unit", "showrunner"):
            self.units[row.id] = owner.name

    def failure(self, key: str, name: str, reason: str) -> None:
        self.issues[key] = FollowUp(self.targets.get(key, name), reason, f"Nate runs {self.retry} again.", operator=True)

    def detached_row_failure(self, key: str, error: BaseException) -> None:
        status = "stopped" if self.command == "agents stop --all" else "back"
        reason = f"Could not confirm the session is {status}. {_error_reason(error)}"
        self.issues[key] = FollowUp(self.targets[key], reason, "Nate sends this error to the efficiency unit.", operator=True)

    def log_repair_step(self) -> str:
        error = self.log_error
        path = shlex.quote(str(self.log.parent))
        cause = error.errno if isinstance(error, OSError) else None
        if cause == errno.EACCES:
            folder = self.log.parent
            with suppress(OSError):
                folder = next((parent for parent in self.log.parents if parent.is_dir()), folder)
            repair = f"gives this account write and search access to {shlex.quote(str(folder))}"
            return (f"Nate {repair}. Nate gives this account search access to the parent folders. "
                    f"Then Nate runs {self.retry} again.")
        elif cause == errno.ENOTDIR:
            blocked = None
            with suppress(OSError):
                blocked = next((parent for parent in self.log.parents if parent.is_file()), None)
            if blocked is None:
                return "Nate sends this error to the efficiency unit."
            repair = f"moves the file at {shlex.quote(str(blocked))} and creates a writable folder at that path"
        elif cause == errno.ENOSPC:
            repair = f"frees space on the filesystem that contains {path}"
        else:
            return "Nate sends this error to the efficiency unit."
        return f"Nate {repair}. Then Nate runs {self.retry} again."

    def finish(self, key: str) -> None:
        first = key not in self.completed
        self.completed.add(key)
        _ = self.issues.pop(key, None)
        if first and self.on_finish is not None:
            self.on_finish(self.targets[key], len(self.targets))

    def mark_not_started(self, keys: Iterable[str], retry: str) -> None:
        step = _plain_terminal_step(retry)
        for key in keys:
            _ = self.not_started.setdefault(key, step)

    def detach_data(self) -> str:
        issues: dict[str, FollowUpFields] = {key: {
            "name": issue.name, "reason": issue.reason, "step": issue.step,
            "unit": issue.unit, "operator": issue.operator, "director_check": issue.director_check,
        } for key, issue in self.issues.items()}
        data: DetachedReport = {
            "retry": self.retry,
            "targets": self.targets,
            "units": self.units,
            "issues": issues,
        }
        return json.dumps(data)

    def receive(self, data: DetachedReport) -> None:
        self.targets.update(data["targets"])
        self.fixed_targets = frozenset(data["targets"])
        self.units.update(data["units"])
        self.issues.update({key: FollowUp(**issue) for key, issue in data["issues"].items()})
        self.inherited.update(self.issues)

    def follow_ups(self, *, dry_run: bool = False, failure: FollowUp | None = None) -> dict[str, FollowUp]:
        status = "stopped" if self.command == "agents stop --all" else "back"
        targets = self.targets.keys() if self.fixed_targets is None else self.fixed_targets
        completed = targets & self.completed
        issues = dict(self.issues)
        for key in self.not_started.keys() - completed:
            earlier = issues.get(key)
            reason = "not started: the restart was stopped"
            if earlier is not None:
                reason = f"{reason}. {earlier.reason}"
            issues[key] = FollowUp(self.targets[key], reason, self.not_started[key], operator=True)
        missing = targets - completed - issues.keys()
        received = self.stop_signal
        if failure is not None and not missing and failure not in issues.values():
            issues["detached"] = failure
        if failure is not None or received is not None:
            for key in completed - issues.keys():
                issues[key] = FollowUp(self.targets[key], f"The session is {status}", "")
        command_issue = issues.get("") if self.fixed_targets is not None else None
        if not dry_run or failure is not None:
            for key in missing:
                cause = failure or command_issue
                reason = f"Could not confirm the session is {status}"
                if cause is not None:
                    reason = f"{reason}. {cause.reason}"
                step = cause.step if cause is not None else f"Nate runs {self.retry} again."
                issues[key] = FollowUp(self.targets[key], reason, step, operator=True)
        return issues

    def lines(self, *, dry_run: bool = False, failure: FollowUp | None = None) -> list[str]:
        status = "stopped" if self.command == "agents stop --all" else "back"
        targets = self.targets.keys() if self.fixed_targets is None else self.fixed_targets
        suffix = " (dry run)" if dry_run else ""
        lines = [f"{self.command}: {len(targets & self.completed)} of {len(targets)} sessions {status}{suffix}.", *self.preview]
        issues = self.follow_ups(dry_run=dry_run, failure=failure)
        lines.extend(item.line() for item in sorted(issues.values(), key=lambda issue: (issue.name, issue.reason)))
        received = self.stop_signal
        actions: list[str] = []
        units = {item.unit or self.units.get(key, "") for key, item in issues.items() if item.operator or item.unit} - {""}
        if units:
            count = len(units)
            actions.append("1 unit needs a step." if count == 1 else f"{count} units need a step.")
        if any(item.unit and not item.director_check for item in issues.values()):
            actions.append("The affected directors run worker.py restore.")
        if any(item.director_check for item in issues.values()):
            actions.append("The affected directors check the continuation message.")
        inherited = self.inherited - self.not_started.keys()
        operator_steps = dict.fromkeys(item.step for key, item in issues.items()
                                       if item.operator and key not in inherited)
        pending = sum(item.operator for key, item in issues.items() if key not in inherited)
        if pending and not units:
            count = "1 item needs a step" if pending == 1 else f"{pending} items need a step"
            actions.append(f"{count}: {' '.join(operator_steps)}")
        elif pending:
            actions.extend(operator_steps)
        elif actions and not any(item.operator for item in issues.values()):
            actions.append("Nate has no step.")
        actions.extend(f"{item.name}: {item.step}" for item in sorted(
            (item for key, item in issues.items() if key in inherited), key=lambda issue: issue.name))
        if received is not None:
            lines.append(f"{self.command}: {received.name} stopped the run.")
        if actions or received is None:
            lines.append(" ".join(actions) or "Nothing for you to do.")
        return lines


_BULK_REPORT: ContextVar[BulkReport | None] = ContextVar("_BULK_REPORT", default=None)
_DETACHED_CHILD: ContextVar[bool] = ContextVar("_DETACHED_CHILD", default=False)


@contextmanager
def use_report(report: BulkReport) -> Generator[None]:
    token = _BULK_REPORT.set(report)
    try:
        yield
    finally:
        _BULK_REPORT.reset(token)


def _create_log(files: ExitStack, path: Path) -> io.FileIO:
    path.parent.mkdir(parents=True, exist_ok=True)
    log = files.enter_context(io.FileIO(path, "xb"))
    _write_log(log, f"{path}\n")
    return log


def _claim_log(log: Path) -> bool:
    path = log.with_suffix(".claim")
    if path.exists():
        return False
    return create_text_atomic(path, f"{os.getpid()}\n")


def _log_claim_pid(log: Path) -> int | None:
    with suppress(OSError, ValueError):
        pid = int(log.with_suffix(".claim").read_bytes())
        if pid > 0:
            return pid
    return None


def _write_log(log: io.FileIO, text: str) -> None:
    data = text.encode("utf-8")
    if log.write(data) != len(data):
        raise OSError("Incomplete log write")
    log.flush()


class StopPlanWriteFailed(Exception):
    """The child could not save the stop plan."""


class DetachedReadinessFailed(OSError):
    """The child could not confirm readiness."""


class DetachedRowsFailed(Exception):
    """One or more session results contain unexpected errors."""


_Result = TypeVar("_Result")


def _collect_restart_results(jobs: Mapping[staff.StaffId, Callable[[], _Result]],
                             record: Callable[[staff.StaffId, Future[_Result]], None]) -> None:
    with ThreadPoolExecutor(max_workers=min(16, len(jobs))) as pool:
        futures = {pool.submit(copy_context().run, job): identity for identity, job in jobs.items()}
        report = _BULK_REPORT.get()
        ready = as_completed(futures) if report is not None and report.reboot else iter(futures)
        for future in ready:
            record(futures[future], future)


def _record_stop_plan(report: BulkReport, rows: Iterable[staff.StaffRow]) -> None:
    names = [f"{report.targets[row.id]} ({row.id})" for row in rows]
    header = "\n".join([f"Sessions to stop ({len(names)}).", *names]) + "\n"
    try:
        log = report.log_stream
        if log is None:
            raise OSError("The log is unavailable")
        _write_log(log, header)
        os.fsync(log.fileno())
        report.stop_plan_recorded = True
    except (OSError, ValueError) as error:
        raise StopPlanWriteFailed(_error_reason(error)) from error


def _background_status(command: str) -> str:
    return f"{command} runs in the background. Read its report in this log."


def _plain_terminal_step(retry: str) -> str:
    return f"Nate opens a plain terminal without a staff session. Nate runs {retry} there."


def _record_retry_step(retry: str) -> str:
    return "Nate waits for the command that holds the restart record lock to finish. " + _plain_terminal_step(retry)


def _report_record_busy(key: str, name: str, error: shutdown_record.RecordBusy, retry: str) -> None:
    step = _record_retry_step(retry)
    if (report := _BULK_REPORT.get()) is None:
        print(f"{name}: restart anomaly: {_error_reason(error)}. {step}", file=sys.stderr)
    else:
        report.issues[key] = FollowUp(report.targets.get(key, name), _error_reason(error), step, operator=True)


def run_command(action: Callable[[], int], command: BulkCommand) -> int:
    """Write the command output to the terminal and the same log."""
    if _DETACHED_CHILD.get():
        return action()
    path = shutdown_state_root() / "logs" / f"{command.verb}-{uuid4().hex}.log"
    report = BulkReport(path, "agents stop --all" if command.verb == "stop" else "agents restart", command.retry())
    with ExitStack() as files:
        try:
            report.log_stream = _create_log(files, path)
            # Show the path before the child can stop the caller.
            print(path, end="", flush=True)
        except (OSError, ValueError) as error:
            report.log_error = error
            print(f"Could not write the log at {path}. {_error_reason(error)}", flush=True)

        output = io.StringIO()
        try:
            with use_report(report), redirect_stdout(output), redirect_stderr(output):
                code = action()
        except UnknownAccountName as error:
            report.issues[""] = FollowUp(report.command, _error_reason(error), "Nate selects an available account.", operator=True)
            code = 2
        except (OSError, ValueError, conf.Unusable, staff.StaffRefused, herdr_types.HerdrFailure,
                subprocess.SubprocessError) as error:
            report.failure("", report.command, _error_reason(error))
            code = 2
        log = report.log_stream
        if report.writer is LogWriter.CHILD:
            print(f"  {_background_status(report.command)}", flush=True)
            return code
        if log is not None:
            print(flush=True)
        if code and not report.issues:
            report.failure("", report.command, "Could not complete the command")
        result = "\n".join(report.lines(dry_run=command.dry_run)) + "\n"
        if log is not None and report.writer is LogWriter.LOCAL:
            try:
                _write_log(log, result)
            except (OSError, ValueError) as error:
                print(f"Could not finish writing the log at {path}. {_error_reason(error)}",
                      file=sys.stderr, flush=True)
        print(result, end="", flush=True)
        return code


@dataclass
class WorkerRestoreReports:
    attempts: dict[staff.StaffId, tuple[staff.StaffRow, RestoreReport]] = field(default_factory=dict)
    command: BulkCommand = BulkCommand()

    def resolved(self, scope: staff.StaffId) -> set[staff.StaffId]:
        attempt = self.attempts.get(scope)
        if attempt is None:
            return set()
        _owner, result = attempt
        return result.restored | (result.skipped.keys() - result.failures.keys())

    def report(self) -> None:
        owners: dict[staff.StaffId, staff.StaffRow] = {}
        scopes: dict[staff.StaffId, dict[staff.StaffId, RestoreReport]] = {}
        for scope, (owner, result) in self.attempts.items():
            owners[owner.id] = owner
            scopes.setdefault(owner.id, {})[scope] = result
        for owner in sorted(owners.values(), key=lambda row: (row.name, row.id)):
            restored: set[staff.StaffId] = set()
            failures: dict[staff.StaffId, tuple[str, str]] = {}
            run_failures: dict[Path, str] = {}
            skipped: dict[staff.StaffId, str] = {}
            record_busy: set[staff.StaffId] = set()
            for result in scopes[owner.id].values():
                restored.update(result.restored)
                failures.update(result.failures)
                run_failures.update(result.run_failures)
                skipped.update(result.skipped)
                record_busy.update(result.record_busy)
            if (bulk := _BULK_REPORT.get()) is not None:
                for identity in restored:
                    _ = bulk.targets.setdefault(identity, str(identity))
                    bulk.finish(identity)
                for identity, (name, reason) in failures.items():
                    bulk.targets[identity] = name
                    bulk.issues[identity] = (
                        FollowUp(f"{owner.name} / {name}", reason,
                            _record_retry_step(replace(self.command, only=(identity,)).retry()), operator=True)
                        if identity in record_busy else FollowUp(f"{owner.name} / {name}", reason,
                            f"The director runs worker.py restore in {owner.name}.", owner.name))
                for identity, reason in skipped.items():
                    if identity in bulk.targets and identity not in restored and identity not in failures:
                        bulk.issues[identity] = FollowUp(bulk.targets[identity], reason,
                            "The director assigns work when needed.")
                if run_failures:
                    reason = "; ".join(f"{run}: {error}" for run, error in sorted(run_failures.items()))
                    bulk.issues[f"run:{owner.id}"] = FollowUp(owner.name, f"Could not reap detached runs: {reason}",
                        f"The director runs worker.py restore in {owner.name}.", owner.name)
                continue
            count = len(restored)
            next_step = f"run worker.py restore in {owner.name}"
            for identity, (name, reason) in sorted(failures.items(), key=lambda item: item[1]):
                status = "restored" if identity in restored else "not restored"
                step = (_record_retry_step(replace(self.command, only=(identity,)).retry())
                        if identity in record_busy else next_step)
                print(f"restart anomaly: {owner.name}: worker {name} {status}: "
                      + f"{' '.join(reason.splitlines())}; {step}", file=sys.stderr)
            for run, reason in sorted(run_failures.items()):
                print(f"restart anomaly: {owner.name}: cannot reap detached runs in {run}: "
                      + f"{' '.join(reason.splitlines())}; {next_step}", file=sys.stderr)
            not_restored = len(failures.keys() - restored)
            if not_restored:
                print(f"{owner.name}: {count} of {count + not_restored} workers restored; see anomaly")
            else:
                noun = "worker" if count == 1 else "workers"
                suffix = "; see anomaly" if failures or run_failures else ""
                print(f"{owner.name}: {count} {noun} restored{suffix}")


@dataclass
class DirectorResumeChecks:
    checks: list[DirectorResumeCheck] = field(default_factory=list)
    replaced_panes: list[ReplacedRestartPane] = field(default_factory=list)
    worker_restores: WorkerRestoreReports = field(default_factory=WorkerRestoreReports)

    def report(self) -> None:
        self.worker_restores.report()
        if not self.checks and not self.replaced_panes:
            return
        try:
            settings = conf.read("shutdown")
            deadline = time.monotonic() + settings.number("RESUME_CHECK_SECONDS")
            interval = settings.number("RESUME_CHECK_INTERVAL_SECONDS")
        except (OSError, ValueError):
            for check in self.checks:
                self.report_check(check, ResumeCheckState.UNREADABLE)
            print(f"restart check: 0 of {len(self.checks)} directors resumed")
            return
        states = [ResumeCheckState.NO_MESSAGE] * len(self.checks)
        while True:
            self.replaced_panes = [pane for pane in self.replaced_panes if not pane.finish()]
            for index, check in enumerate(self.checks):
                if states[index] is not ResumeCheckState.RESUMED:
                    states[index] = check.progress()
            if not self.replaced_panes and all(state is ResumeCheckState.RESUMED for state in states):
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(interval, remaining))
        for check, state in zip(self.checks, states):
            if state is not ResumeCheckState.RESUMED:
                self.report_check(check, state)
        passed = sum(state is ResumeCheckState.RESUMED for state in states)
        if self.checks:
            print(f"restart check: {passed} of {len(self.checks)} directors resumed")
        for pane in self.replaced_panes:
            _anomaly(pane.source.row, f"source pane {pane.source.client.session}:"
                     + f"{pane.source.publication.entry['pane_id']} still has work after the restart check")

    def report_check(self, check: DirectorResumeCheck, state: ResumeCheckState) -> None:
        if (bulk := _BULK_REPORT.get()) is not None:
            _ = bulk.issues.setdefault(f"check:{check.name}", FollowUp(check.name, state.value,
                "The director checks the continuation message.", check.name, director_check=True))
        else:
            print(f"{check.name}: restart check: {state.value}")


class ClaudeTrustWriteFailed(OSError):
    """The shared launch already reported its failed trust write."""


class WorkerRow(Protocol):
    row: staff.StaffRow


class WorkerPanes(Protocol):
    StoppedWorker: type[WorkerRow]
    ClosedWorker: type[WorkerRow]
    def observe(self, parent: staff.StaffId, record: staff.StaffSnapshot) -> list[WorkerRow]: ...
    SelectedWorkerRows: type[WorkerRowSelection]
    def observe_parents(self, parents: list[staff.StaffRow], record: staff.StaffSnapshot, *,
                        selection: WorkerRowSelection | AllWorkers = ...
                        ) -> dict[staff.StaffId, list[WorkerRow]]: ...


class RestoreSkip(Protocol):
    @property
    def reason(self) -> str: ...


class WorkerRestore(Protocol):
    def not_restored(self, row: staff.StaffRow, record: staff.StaffSnapshot, *,
                     runs: dict[str, list[str]] | None = None) -> RestoreSkip | None: ...


class AgentsCommand(Protocol):
    def main(self, argv: list[str]) -> int: ...


class MemberLifecycle(Protocol):
    def detach(self, arguments: list[str], task: str, *, program: Path, log: Path | None = None,
               log_fd: int | None = None) -> Path: ...
    def stop(self, session: str, *, deadline: float,
             codex: herdr.CodexConversations | None = None) -> StopResult: ...
    def stop_unit_workers(self, unit: staff.StaffId, record: staff.StaffSnapshot, *,
                          codex: herdr.CodexConversations | None = None,
                          keep_running: frozenset[staff.StaffId] = ...,
                          failures: dict[staff.StaffId, str] | None = None) -> list[str]: ...


worker_panes = cast(WorkerPanes, cast(object, import_module("scripts.herdr.worker_panes")))
stop_resume = cast(MemberLifecycle, cast(object, import_module("scripts.agents.stop_resume")))


@dataclass(frozen=True)
class RegistryConfirmation:
    records: tuple[sessions.SessionRecord, ...]

    @property
    def live_ids(self) -> frozenset[str]:
        return frozenset(record["sessionId"] for record in self.records)


def wait_for_confirmation(session_ids: frozenset[str], timeout: float) -> RegistryConfirmation:
    deadline = time.monotonic() + timeout
    while True:
        records = tuple(record for record in sessions.live_sessions() if record["sessionId"] in session_ids)
        if session_ids <= {record["sessionId"] for record in records} or time.monotonic() >= deadline:
            return RegistryConfirmation(records)
        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))


def _error_reason(error: object) -> str:
    reason = str(error).splitlines()
    return reason[0] if reason else type(error).__name__


def _launch_failure_reason(error: Exception) -> str:
    if isinstance(error, subprocess.TimeoutExpired):
        return "The launcher timed out before the detached process claimed the log"
    if isinstance(error, FileNotFoundError) and cast(object, error.filename) == "systemd-run":
        return "The systemd-run command is missing"
    if isinstance(error, subprocess.CalledProcessError):
        detail = cast(str | None, error.stderr)
        if detail and detail.strip():
            return detail.strip()
        if error.returncode < 0:
            return f"A signal stopped the launcher (signal {-error.returncode})"
        return f"The launcher returned exit status {error.returncode}"
    return _error_reason(error)


def _anomaly(row: staff.StaffRow, error: object, *, retry: str = "") -> None:
    if (report := _BULK_REPORT.get()) is not None:
        report.failure(row.id, row.name, _error_reason(error))
    else:
        step = f". Run {retry} again." if retry and isinstance(error, staff.UpkeepRowBusy) else ""
        print(f"{row.name}: restart anomaly: {_error_reason(error)}{step}", file=sys.stderr)


def _discovery_anomaly(reason: str) -> None:
    if (report := _BULK_REPORT.get()) is not None:
        report.failure("", report.command, reason)
    else:
        print(f"restart: {reason}", file=sys.stderr)


def stop_row(row: staff.StaffRow, client: herdr.Herdr, *, deadline: float = math.inf,
             codex: herdr.CodexConversations | None = None) -> bool:
    # One row's stop reads each pane once; each identity check forgets its pane before it reads again.
    with operation_deadline.within(deadline), herdr.observation_snapshot():
        try:
            record = staff.initial_snapshot()
            if record.by_id(row.id) is None:
                record = staff.StaffSnapshot((*record.rows, row))
            panes = pane_launch.reference_panes(pane_launch.StaffLaunchReference(row.id), record, client,
                pane_record.read(client.session))
            if not panes and any(pane["session_id"] == row.current_session for pane in client.panes()):
                raise herdr_types.HerdrFailure(f"would signal a process that does not run session {row.current_session}",
                                               "wrong_target")
            if len(panes) > 1:
                _anomaly(row, "duplicate panes retained; no signal sent")
                return False
            if panes and not herdr.check_target(str(row.current_session), action="signal",
                    place=herdr.HerdrPlace(client.session, str(panes[0]["pane_id"])),
                    terminal=str(panes[0].get("terminal_id", "")), client=client):
                return False
            if panes:
                # End the restore entry before the signal, so no restore revives the stopped session.
                pane_record.end(client.session, str(panes[0]["pane_id"]), str(row.current_session),
                                record=staff.StaffSnapshot((row,)))
        except (herdr_types.HerdrFailure, OSError, ValueError) as error:
            _anomaly(row, error)
            return False
        shared: StopReadArguments = {"codex": codex} if codex is not None else {}
        result = stop_resume.stop(str(row.current_session), deadline=deadline, **shared)
        if isinstance(result, (Stopped, NotRunning)):
            return True
        _anomaly(row, result.reason if isinstance(result, StopFailed) else "session not found")
        return False


def _saved_launch(entry: pane_record.PaneLaunch, identity: str, name: str, *,
                  record: staff.StaffSnapshot | pane_launch.StaffSnapshotRead = pane_launch.StaffSnapshotRead.CURRENT) -> tuple[Literal["claude", "codex"], dict[str, str], list[str]]:
    record = staff.initial_snapshot() if isinstance(record, pane_launch.StaffSnapshotRead) else record
    projected = pane_launch.current_launch(entry, record)
    if "staff_id" not in projected:
        projected["session_id"] = identity
        projected["name"] = name
    arguments = pane_record.resume_arguments(projected)
    kind = pane_launch.saved_kind(projected)
    home = pane_launch.config_home(projected, record=record)
    if isinstance(home, staff.HomeUnknown):
        print(f"anomaly: {identity}: session home is unknown; select the normal account", file=sys.stderr)
        return kind, {}, arguments
    environment = {"CODEX_HOME": str(home.path)} if kind == "codex" else named_account_environment(home.path)
    return kind, environment, arguments


def producer_first_arguments(name: str) -> tuple[Literal["claude", "codex"], list[str]]:
    """Select the configured producer settings without a resume identity."""
    settings = models_registry.launch_settings("producers", name)
    if settings.kind == "codex":
        arguments = ["--model", settings.model,
                     "-c", f'model_reasoning_effort="{settings.effort}"']
        if settings.service_tier:
            arguments.extend(("-c", f'service_tier="{settings.service_tier}"'))
    else:
        arguments = ["--name", name, "--remote-control", name,
                     "--model", settings.model, "--effort", settings.effort,
                     "--settings", '{"disableAgentView": true}']
    return settings.kind, arguments


def producer_launch(row: staff.StaffRow) -> tuple[Literal["claude", "codex"], dict[str, str], list[str]]:
    """Resume the producer's current conversation with its configured settings."""
    kind, arguments = producer_first_arguments(row.name)
    return kind, {}, ["resume" if kind == "codex" else "--resume", str(row.current_session), *arguments]


def producer_resume(row: staff.StaffRow, entry: pane_record.PaneLaunch
                    ) -> tuple[Literal["claude", "codex"], dict[str, str], list[str]]:
    """Keep saved launch options with the producer's current model settings; a Claude producer runs with Remote Control."""
    settings = models_registry.launch_settings("producers", row.name)
    if settings.kind == "claude":
        entry = entry.copy()
        entry["remote_control"] = True
    return settings.kind, {}, pane_record.configured_resume_arguments(entry, str(row.current_session), row.name, settings)


def session_launch(identity: str, name: str) -> tuple[Literal["claude", "codex"], dict[str, str], list[str]]:
    try:
        entry = pane_record.launch_for_session(identity)
    except (OSError, ValueError) as error:
        print(f"{identity}: launch record anomaly: {str(error).splitlines()[0]}", file=sys.stderr)
        entry = None
    if entry is not None:
        return _saved_launch(entry, identity, name)
    for directory in sessions.config_dirs():
        if transcript_paths(directory / "projects", f"*/{identity}.jsonl"):
            turn = pane_record.claude_turn_settings(identity, directory)
            arguments = ["--resume", identity, "--name", name, *pane_record.claude_resume_settings(turn).arguments()]
            return "claude", named_account_environment(directory), arguments
    for directory in codex_transcript_homes():
        if transcript_paths(directory / "sessions", f"**/rollout-*{identity}.jsonl"):
            return "codex", {"CODEX_HOME": str(directory)}, ["resume", identity]
    raise OSError(f"no transcript for {identity}")


@dataclass(frozen=True)
class PaneConversations:
    codex: herdr.CodexConversations
    names: dict[str, str]


@dataclass(frozen=True)
class RestartObservations:
    codex: herdr.CodexConversations
    runs: dict[str, list[str]]
    pane_owners: frozenset[staff.StaffId]
    pane_names: dict[str, str]


def _observations(record: staff.StaffSnapshot, client: herdr.Herdr) -> RestartObservations:
    owners = frozenset(row.id for row in record.rows if row.kind in ("unit", "showrunner")
                       and record.find(kind="worker", parent=row.id))
    runs = unit_runs.held_by_session(os.environ.get("ROSTER_RUN_ROOT"), record=record) if owners else {}
    panes = _codex_conversations(client)
    return RestartObservations(panes.codex, runs, owners, panes.names)


def leaves_record(row: staff.StaffRow, record: staff.StaffSnapshot, *,
                  runs: dict[str, list[str]] | None = None) -> str | None:
    """Name why this row never restarts, so its id leaves the restart record; None when it can restart."""
    if row.kind != "worker":
        return None
    worker = cast(WorkerRestore, cast(object, import_module("scripts.production.worker")))
    skip = worker.not_restored(row, record, runs=runs)
    return None if skip is None else skip.reason


def _restore_workers(row: staff.StaffRow, record: staff.StaffSnapshot, *,
                     observed: RestartObservations | None = None,
                     target_account: str | accounts.AccountSelection = "",
                     reports: WorkerRestoreReports | None = None) -> str:
    from scripts.herdr.worker_panes import defer_worker_anomalies
    from scripts.production import worker

    started_panes: dict[staff.StaffId, str] = {}
    only: frozenset[staff.StaffId] | Literal["all"] = "all"
    if row.kind == "worker":
        parent = record.parent_of(row)
        if parent is None:
            raise OSError("worker parent is unavailable")
        members = [row]
        only = frozenset({row.id})
    elif row.kind in ("unit", "showrunner"):
        if (bulk := _BULK_REPORT.get()) is not None:
            bulk.finish(row.id)
        parent = row
        members = record.find(kind="worker", parent=parent.id)
        if not members:
            return ""
    else:
        return ""
    report = worker.RestoreReport()
    try:
        with defer_worker_anomalies():
            result = worker.restore(parent.id, record=record, only=only, started_panes=started_panes,
                account=target_account if only != "all" else "",
                runs=observed.runs if observed is not None else None,
                codex=observed.codex if observed is not None else herdr.CodexLaunchRead.CURRENT, report=report)
    except (OSError, ValueError, staff.StaffRefused, herdr_types.HerdrFailure, subprocess.SubprocessError) as error:
        for member in members:
            if member.id not in report.restored:
                _ = report.failures.setdefault(member.id, (member.name, _error_reason(error)))
        result = worker.ExitCode.UNCERTAIN
    if (bulk := _BULK_REPORT.get()) is not None and bulk.reboot:
        for member in members:
            if member.id in report.restored:
                bulk.track(member, record)
                bulk.finish(member.id)
    if reports is None:
        account = target_account if isinstance(target_account, str) else target_account.label
        WorkerRestoreReports({row.id: (parent, report)}, BulkCommand(account=account)).report()
    else:
        reports.attempts[row.id] = parent, report
    if result != worker.ExitCode.OK:
        raise WorkerRestoreIncomplete(f"run worker.py restore in {parent.name}")
    return started_panes.get(row.id, "")


def pane_holds_other_work(client: herdr.Herdr, pane_id: str, identity: str) -> bool:
    fresh = next((pane for pane in client.panes() if pane["pane_id"] == pane_id), None)
    if fresh is None:
        return False
    activity = client.pane_activity(pane_id)
    if ((fresh["session_id"] and fresh["session_id"] != identity)
            or not isinstance(activity, herdr.PaneAtShell)):
        print(f"{pane_id}: pane close anomaly: pane holds other work; kept open", file=sys.stderr)
        return True
    return False


def start_row(row: staff.StaffRow, record: staff.StaffSnapshot, client: herdr.Herdr, *,
              present: bool = False, target_account: str | accounts.AccountSelection = "", deadline: float = math.inf,
              resolver: session_staff.RowResolver | None = None,
              worker_scope: WorkerRestartScope = WorkerRestartScope.FOLLOW_OWNER,
              observed: RestartObservations | None = None,
              opening: herdr.OpeningPrompt | herdr.NoOpeningPrompt = NO_RESTART_MESSAGE,
              resume_checks: DirectorResumeChecks | NoDirectorResumeChecks = NoDirectorResumeChecks.CURRENT,
              live_worker_recovery: LiveWorkerRecovery = LiveWorkerRecovery.LEAVE_RUNNING,
              success_report: RestartSuccessReport = RestartSuccessReport.START_ROW) -> str:
    restore_workers = (partial(_restore_workers, reports=resume_checks.worker_restores)
                       if isinstance(resume_checks, DirectorResumeChecks) else _restore_workers)
    with operation_deadline.within(deadline):
        live = (herdr.read_conversation(str(row.current_session)) if observed is None else
                herdr.read_conversation(str(row.current_session), codex=observed.codex))
        if isinstance(live, herdr.LiveConversation):
            if live.pids:
                member_account.repair_home(row, live.kind, live.pids[0])
            if row.kind == "worker" and live_worker_recovery is LiveWorkerRecovery.RESTORE_BEFORE_REMOVAL:
                return restore_workers(row, record, observed=observed, target_account=target_account)
            if row.kind != "worker" and worker_scope is WorkerRestartScope.FOLLOW_OWNER:
                _ = (restore_workers(row, record) if observed is None else
                     restore_workers(row, record, observed=observed))
            print(f"{row.name}: unchanged")
            return ""
        if isinstance(live, herdr.UnknownConversation):
            herdr.conversation_note(live)
        if row.kind == "worker":
            return restore_workers(row, record, observed=observed, target_account=target_account)
        machine, folder = shutdown_record.row_folder(row, record, resolver=resolver)
        from scripts.agents import remote
        if machine != remote.local_machine():
            raise ValueError(f"{row.name}: session is on {machine}")
        from scripts.agents.launch_names import LaunchNames

        names = LaunchNames(record)
        publication = pane_record.publication_for_session(str(row.current_session), record=record)
        original = publication.entry if publication is not None else None
        projected = names.resume(original) if original is not None else None
        if row.kind == "producer":
            kind, environment, arguments = producer_resume(row, original) if original is not None else producer_launch(row)
        else:
            kind, environment, arguments = (_saved_launch(original, str(row.current_session), row.name, record=record)
                if original is not None else session_launch(str(row.current_session), row.name))
        selected = member_account.resume_account(kind, row, to=target_account)
        if isinstance(selected, accounts.NamedAccount):
            environment = named_account_environment(selected.dir)
        else:
            environment = {"CODEX_HOME": str(selected.home)}
        if projected is not None:
            environment = {**names.environment(projected), **environment}
        settings = conf.read("shutdown")
        start_seconds = settings.number("START_SECONDS")
        reuse_deadline = time.monotonic() + operation_deadline.remaining(start_seconds)
        workspace = herdr.row_workspace(row, record, row.name)
        destination = (client.producer_row_tab(str(row.id), row.name, workspace, record=record) if row.kind == "producer" else
                       client.saved_row_tab(str(row.id), row.name, workspace))
        selected_home = selected.dir if isinstance(selected, accounts.NamedAccount) else selected.home
        expectation = pane_launch.ProducerLaunchRequest(row.id, row.current_session, kind, Path(folder), selected_home)
        if row.kind == "producer":
            environment = expectation.environment(environment)
        prepared = herdr.prepare_session_environment(str(row.current_session), str(folder), environment,
                                                      herdr.NO_ENVIRONMENT_REMOVALS)
        if isinstance(prepared, nix_env.Refused):
            raise OSError(str(prepared))
        target: herdr.NewTab | herdr.ExistingPane = destination
        source_pane: pane_launch.ResumeSourcePane | pane_launch.ResumeSourceAbsent = pane_launch.ResumeSourceAbsent.CURRENT
        exiting_source: RetainedRestartPane | pane_launch.ResumeSourceAbsent = pane_launch.ResumeSourceAbsent.CURRENT
        source_client = client
        try:
            if publication is not None and publication.path != pane_record.record_path(client.session):
                source_client = herdr.Herdr(publication.path.stem)
            entries = ({original["pane_id"]: original} if original is not None else {})
            locations = [dict(pane) for pane in source_client.panes()]
            evidence = pane_launch.NativePaneEvidence(locations, source_client.scan_row_panes(record, locations))
            panes = pane_launch.reference_panes(pane_launch.StaffLaunchReference(row.id), record, source_client,
                                                entries, evidence=evidence)
            if not panes and publication is not None and original is not None:
                retained = next((pane for pane in locations if pane["pane_id"] == original["pane_id"]
                                 and any(unknown.pane == pane["pane_id"] for unknown in evidence.scan.unknown)), None)
                if retained is not None:
                    exiting_source = RetainedRestartPane(row, publication, source_client, str(retained.get("terminal_id", "")))
                    state = exiting_source.state()
                    while state is RetainedPaneState.EXITING:
                        remaining = reuse_deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        time.sleep(operation_deadline.remaining(min(0.1, remaining)))
                        state = exiting_source.state()
                    source_pane = pane_launch.ResumeSourcePane(original["pane_id"])
                    if state is RetainedPaneState.SHELL:
                        panes = [retained]
                        if row.kind == "producer" and source_client.session == client.session:
                            target = client.producer_row_tab(str(row.id), row.name, workspace, record=record)
                    elif state is RetainedPaneState.FOREIGN:
                        source_pane = pane_launch.ResumeSourceAbsent.OTHER_OWNER
            if len(panes) > 1:
                _anomaly(row, "duplicate panes retained")
            elif panes:
                pane = panes[0]
                source_pane = pane_launch.ResumeSourcePane(str(pane["pane_id"]))
                if (row.kind != "producer" and source_client.session == client.session
                        and pane.get("terminal_id")
                        and (pane in evidence.scan.found.get(row.id, ())
                             or isinstance(client.pane_activity(str(pane["pane_id"])), herdr.PaneAtShell))):
                    target = herdr.ExistingPane(str(pane["pane_id"]), str(pane["terminal_id"]))
            elif original is not None and not any(pane["pane_id"] == original["pane_id"] for pane in locations):
                source_pane = pane_launch.ResumeSourcePane(original["pane_id"])
            elif original is not None and any(pane["pane_id"] == original["pane_id"]
                    for owner_id, matches in evidence.scan.found.items() if owner_id != row.id for pane in matches):
                source_pane = pane_launch.ResumeSourceAbsent.OTHER_OWNER
        except (OSError, ValueError, herdr_types.HerdrFailure) as error:
            _anomaly(row, error)
        # Read liveness again immediately before the launch.
        current = (herdr.read_conversation(str(row.current_session), kind) if observed is None else
                   herdr.read_conversation(str(row.current_session), kind, codex=observed.codex))
        if isinstance(current, herdr.LiveConversation):
            if current.pids:
                member_account.repair_home(row, current.kind, current.pids[0])
            if worker_scope is WorkerRestartScope.FOLLOW_OWNER:
                _ = (restore_workers(row, record) if observed is None else
                     restore_workers(row, record, observed=observed))
            print(f"{row.name}: unchanged")
            return ""
        if isinstance(current, herdr.UnknownConversation) and not isinstance(live, herdr.UnknownConversation):
            herdr.conversation_note(current)
        operation_deadline.check()
        check: DirectorResumeCheck | NoDirectorResumeChecks = NoDirectorResumeChecks.CURRENT
        if isinstance(opening, herdr.OpeningPrompt):
            transcript = herdr.SessionTranscript(selected_home, str(row.current_session))
            opening = replace(opening, transcript=transcript)
            since = datetime.now(timezone.utc)
            if kind == "claude":
                opening = opening.prepare_for_launch(since)
            if row.kind in ("unit", "showrunner") and isinstance(resume_checks, DirectorResumeChecks):
                reader = (cast(herdr.TranscriptCursor, opening.cursor) if kind == "claude" else
                          CodexResumeTranscript(transcript, since))
                if isinstance(reader, CodexResumeTranscript):
                    reader.skip_existing_turns()
                progress = DirectorResumeCheck(row.name, opening.text, reader)
                check = progress
                if isinstance(reader, herdr.TranscriptCursor):
                    cursor = reader
                    before_prompt = opening.before_prompt

                    def native_transcript(actual: herdr.SessionTranscript) -> object:
                        if cursor.transcript != actual:
                            progress.reader = herdr.TranscriptCursor(actual, cursor.since)
                        return before_prompt(actual)

                    opening = replace(opening, before_prompt=native_transcript)
        scope = (pane_launch.producer_launch_request(expectation)
                 if row.kind == "producer" else nullcontext())
        with scope:
            launch_seconds = (max(start_seconds, settings.number("OPENING_PROMPT_SECONDS"))
                              if kind == "claude" and isinstance(opening, herdr.OpeningPrompt) else start_seconds)
            result = herdr.launch_prepared_agent(client.session, target, prepared, kind,
                trust_arguments(kind, str(folder), arguments),
                opening if kind == "claude" else NO_RESTART_MESSAGE,
                int(operation_deadline.remaining(launch_seconds) * 1000), agent=row.name,
                reported_unknown=(current.reason if isinstance(current, herdr.UnknownConversation) else
                                  live.reason if isinstance(live, herdr.UnknownConversation) else ""),
                codex=observed.codex if observed is not None else herdr.CodexLaunchRead.CURRENT, client=client)
        if isinstance(result.outcome, herdr.ClaudeTrustNotWritten):
            raise ClaudeTrustWriteFailed(result.outcome.reason)
        if not isinstance(result.outcome, herdr.PromptSubmissionNotRequired) and result.outcome.publication_error is not None:
            raise OSError(f"staff publication failed: {result.outcome.publication_error}")
        if (not isinstance(result.outcome, herdr.PromptSubmissionNotRequired)
                and result.outcome.state != "confirmed"):
            if isinstance(target, herdr.ExistingPane) and not target.terminal:
                return result.pane
            raise OSError(result.outcome.reason)
        if kind == "codex" and isinstance(opening, herdr.OpeningPrompt):
            try:
                client.prompt(result.pane, opening.text, target=str(row.current_session))
            except (OSError, ValueError, herdr_types.HerdrFailure, subprocess.SubprocessError) as error:
                reason = f"Could not send the continuation message: {_error_reason(error)}"
                if (bulk := _BULK_REPORT.get()) is not None:
                    bulk.issues[f"check:{row.name}"] = FollowUp(row.name, reason,
                        "The director checks the continuation message.", row.name, director_check=True)
                else:
                    _anomaly(row, f"continuation message was not sent: {error}")
        if row.kind == "producer":
            home = selected.dir if isinstance(selected, accounts.NamedAccount) else selected.home
            try:
                _ = staff.set_home(row.id, staff.SessionHome(home.absolute()))
            except (staff.StaffUnreadable, staff.StaffNotFound) as error:
                _anomaly(row, error)
        path = pane_record.record_path(client.session)
        if (publication is not None and original is not None and projected is not None
                and (publication.path != path or result.pane != original["pane_id"])):
            try:
                if isinstance(exiting_source, RetainedRestartPane):
                    completed = expectation.expected_launch(projected) if row.kind == "producer" else projected
                    cleanup = ReplacedRestartPane(exiting_source, client, path,
                                                  pane_launch.resume_attempt(path, result.pane),
                                                  names.resume_proof(original, completed))
                    if not cleanup.finish():
                        if isinstance(resume_checks, DirectorResumeChecks):
                            resume_checks.replaced_panes.append(cleanup)
                        else:
                            DirectorResumeChecks(replaced_panes=[cleanup]).report()
                elif (isinstance(source_pane, pane_launch.ResumeSourceAbsent)
                        or not pane_holds_other_work(source_client, source_pane.pane, str(row.current_session))):
                    completed = expectation.expected_launch(projected) if row.kind == "producer" else projected
                    pane_launch.finish_restore(publication.path, original,
                                               pane_launch.resume_attempt(path, result.pane),
                                               names.resume_proof(original, completed), close=client.close_tab_confirmed,
                                               destination=path, close_source=source_client.close_tab_confirmed,
                                               source_pane=source_pane)
            except (OSError, ValueError, herdr_types.HerdrFailure) as error:
                _anomaly(row, error)
        try:
            client.remember_row_tab(str(row.id), result.pane)
            if present:
                client.focus_tab_of(result.pane)
                client.close_startup_workspaces(result.pane.split(":", 1)[0])
        except (OSError, ValueError, herdr_types.HerdrFailure, subprocess.SubprocessError) as error:
            _anomaly(row, error)
        if isinstance(resume_checks, DirectorResumeChecks) and isinstance(check, DirectorResumeCheck):
            resume_checks.checks.append(check)
        if worker_scope is WorkerRestartScope.FOLLOW_OWNER:
            _ = (restore_workers(row, record) if observed is None else
                 restore_workers(row, record, observed=observed))
        if success_report is RestartSuccessReport.START_ROW:
            print(f"{row.name}: restarted")
        return result.pane


@dataclass(frozen=True)
class RowPanesUnreadable:
    reason: str


def _read_row_panes(rows: list[staff.StaffRow], client: herdr.Herdr, *, strict: bool
                    ) -> list[tuple[staff.StaffRow, str]] | RowPanesUnreadable:
    try:
        entries = pane_record.read_strict(client.session) if strict else pane_record.read(client.session)
        record = staff.initial_snapshot()
        record = staff.StaffSnapshot((*record.rows, *(row for row in rows if record.by_id(row.id) is None)))
        with herdr.pane_evidence_run():
            panes = [dict(pane) for pane in client.panes()]
            evidence = pane_launch.NativePaneEvidence(panes, client.scan_row_panes(record, panes))
            found: list[tuple[staff.StaffRow, str]] = []
            for row in rows:
                reference = pane_launch.StaffLaunchReference(row.id)
                newest = max((entry for entry in entries.values() if pane_launch.launch_reference(entry, record) == reference),
                             key=lambda entry: entry["written_at"], default=None)
                retained = {newest["pane_id"]: newest} if newest is not None else {}
                found.extend((row, str(pane["pane_id"])) for pane in pane_launch.reference_panes(
                    reference, record, client, retained, evidence=evidence, strict_retained_reads=strict))
        return found
    except (OSError, ValueError, herdr_types.HerdrFailure, subprocess.SubprocessError) as error:
        return RowPanesUnreadable(_error_reason(error))


def read_row_panes(rows: list[staff.StaffRow], client: herdr.Herdr) -> list[tuple[staff.StaffRow, str]] | RowPanesUnreadable:
    return _read_row_panes(rows, client, strict=True)


def row_panes(rows: list[staff.StaffRow], client: herdr.Herdr) -> list[tuple[staff.StaffRow, str]]:
    result = _read_row_panes(rows, client, strict=False)
    if isinstance(result, RowPanesUnreadable):
        print(f"restart: layout anomaly: {result.reason}", file=sys.stderr)
        return []
    return result


@dataclass(frozen=True)
class CompleteLiveRows:
    rows: list[staff.StaffRow]


@dataclass(frozen=True)
class PartialLiveRows:
    rows: list[staff.StaffRow]
    registry_discovery_failed: bool = False


def _read_live_members(rows: Iterable[staff.StaffRow], record: staff.StaffSnapshot, *,
                 resolver: session_staff.RowResolver | None = None,
                 codex: herdr.CodexConversations | None = None,
                 account: AllRestartAccounts | accounts.AccountSelection = ALL_RESTART_ACCOUNTS
                 ) -> CompleteLiveRows | PartialLiveRows:
    """Read only the supplied rows and return their live local conversations."""
    from scripts.agents import remote

    selected: list[staff.StaffRow] = []
    complete = True
    shared_unknown = 0
    hosted = {session.thread_id: read.owner.home
              for read in codex_daemon.read_daemons(os.environ)
              if not isinstance(read.sessions, codex_daemon.DaemonUnreadable)
              for session in read.sessions} if not isinstance(account, AllRestartAccounts) else {}
    resolver = session_staff.RowResolver(record) if resolver is None else resolver
    for row in rows:
        try:
            machine, _folder = shutdown_record.row_folder(row, record, resolver=resolver)
            if machine != remote.local_machine():
                continue
            if codex is None:
                observed = herdr.read_conversation(str(row.current_session), "claude")
                if not isinstance(observed, herdr.LiveConversation):
                    codex = herdr.codex_conversations()
                    herdr.codex_notes(codex)
                    observed = herdr.read_conversation(str(row.current_session), codex=codex)
            else:
                observed = herdr.read_conversation(str(row.current_session), codex=codex)
            if isinstance(observed, herdr.LiveConversation):
                if (isinstance(account, AllRestartAccounts)
                        or any(account_matches(observed.kind, herdr.process_environment(pid), account)
                               for pid in observed.pids)
                        or not observed.pids and observed.kind == "codex"
                        and isinstance(account, accounts.CodexWorkersAccount)
                        and (home := hosted.get(str(row.current_session))) is not None
                        and home.resolve() == account.home.resolve()):
                    selected.append(row)
                elif row.kind == "worker" and not observed.pids and (
                        observed.kind == "claude" or str(row.current_session) not in hosted):
                    print(f"{row.name}: left alone; account unknown")
            elif isinstance(observed, herdr.UnknownConversation):
                complete = False
                if (report := _BULK_REPORT.get()) is not None:
                    report.track(row, record)
                    report.failure(row.id, row.name, observed.reason)
                if observed.shared_codex:
                    shared_unknown += 1
                if not observed.shared_codex or os.environ.get("SHUTDOWN_DETAIL") == "1":
                    herdr.conversation_note(observed)
        except (OSError, ValueError) as error:
            complete = False
            if (report := _BULK_REPORT.get()) is not None:
                report.track(row, record)
            _anomaly(row, error)
    if shared_unknown:
        print(f"anomaly: {shared_unknown} rows not proved live or stopped (shared cause above); not blocking", file=sys.stderr)
    return CompleteLiveRows(selected) if complete else PartialLiveRows(selected)


def live_members(rows: Iterable[staff.StaffRow], record: staff.StaffSnapshot, *,
                 resolver: session_staff.RowResolver | None = None,
                 codex: herdr.CodexConversations | None = None,
                 account: AllRestartAccounts | accounts.AccountSelection = ALL_RESTART_ACCOUNTS) -> list[staff.StaffRow]:
    """Return proved live members for callers that do not replace restart records."""
    return _read_live_members(rows, record, resolver=resolver, codex=codex, account=account).rows


def _codex_conversations(client: herdr.Herdr) -> PaneConversations:
    """Read panes once for native Codex identities and report names."""
    live: dict[str, tuple[int, ...]] = {}
    names: dict[str, str] = {}
    resolved_tabs: set[str] = set()
    try:
        panes = client.listed("pane")
    except (OSError, ValueError, herdr_types.HerdrFailure):
        return PaneConversations(herdr.codex_conversations(), names)
    try:
        tabs = {herdr.json_text(tab.get("tab_id"), "tab id"):
                herdr.json_text(tab.get("label") or "", "tab label") for tab in client.listed("tab")}
    except (OSError, ValueError, herdr_types.HerdrFailure):
        tabs = {}
    for pane in panes:
        try:
            pane_id = herdr.json_text(pane.get("pane_id"), "pane id")
            tab_name = tabs.get(str(pane.get("tab_id", "")), "")
            label = herdr.json_text(pane.get("label") or "", "pane label")
            display = f"{tab_name} {label} ({pane_id})".strip()
            if identity := herdr.pane_session(pane):
                names[identity] = display
            if pane.get("agent") != "codex":
                continue
            info = herdr.json_object(client.result("pane", "process-info", "--pane", pane_id).get("process_info"),
                                     "native pane processes")
            identity = herdr.pane_native_process(info, "codex", pane).current_identity
            live[identity] = ()
            names[identity] = display
            resolved_tabs.add(f"{tab_name} (Codex thread unknown)")
        except (OSError, ValueError, herdr_types.HerdrFailure):
            continue
    observed = herdr.codex_conversations()
    return PaneConversations(herdr.CodexConversations(
        {**live, **observed.live}, tuple(reason for reason in observed.unknown if reason not in resolved_tabs)), names)


@herdr.observation_snapshot()
def live_rows(record: staff.StaffSnapshot, client: herdr.Herdr, *, save_tabs: bool = False,
              resolver: session_staff.RowResolver | None = None,
              account: AllRestartAccounts | accounts.AccountSelection = ALL_RESTART_ACCOUNTS,
              observed: RestartObservations | None = None,
              selection: SelectedRestartRows | AllRestartRows = AllRestartRows.CURRENT
              ) -> CompleteLiveRows | PartialLiveRows:
    from scripts.herdr.worker_panes import UncertainWorker, ProcessUnknownWorker

    observed = _observations(record, client) if observed is None else observed
    resolver = session_staff.RowResolver(record) if resolver is None else resolver
    codex = observed.codex
    rows = [row for row in record.rows if not isinstance(selection, SelectedRestartRows) or row.id in selection.ids]
    with ExitStack() as selection_scope:
        if isinstance(selection, AllRestartRows):
            try:
                registry = session_records.read_registry()
            except (OSError, ValueError) as error:
                _discovery_anomaly(f"session registry discovery failed: {error}")
                return PartialLiveRows([], registry_discovery_failed=True)
            if registry.unreadable:
                _discovery_anomaly(session_records.registry_unreadable_reason(registry.unreadable))
                return PartialLiveRows([], registry_discovery_failed=True)
            running = session_records.enumerate_running_sessions(registry=registry)
            if isinstance(running, session_records.RunningSessionDiscoveryFailed):
                _discovery_anomaly(running.reason)
                return PartialLiveRows([], registry_discovery_failed=True)
            _ = selection_scope.enter_context(session_records.use_registry_for_liveness(registry))
            candidates = {session["sessionId"]: (session["name"] or session["sessionId"]) +
                (f" in {folder}" if (folder := session.get("cwd")) else "") for session in running.records}
            for identity in herdr.running_codex_threads(thread_host=herdr.codex_managed_daemon, codex=codex):
                _ = candidates.setdefault(identity, identity)
            owned = {row.current_session for row in record.rows}
            for identity in sorted(candidates.keys() - owned):
                if isinstance(herdr.read_conversation(identity, codex=codex), herdr.LiveConversation):
                    name = " ".join(observed.pane_names.get(identity, candidates[identity]).splitlines())
                    if (report := _BULK_REPORT.get()) is not None:
                        report.issues[f"unassigned:{identity}"] = FollowUp(name, "The live session has no staff row",
                            "Nate checks who owns this session.", operator=True)
                    else:
                        print(f"{name}: restart anomaly: unassigned live session", file=sys.stderr)
            owners = [row for row in record.rows if row.id in observed.pane_owners]
            observations = worker_panes.observe_parents(owners, record) if owners else {}
        else:
            parents = {parent.id for row in rows if row.kind == "worker"
                       and (parent := record.parent_of(row)) is not None}
            owners = [row for row in record.rows if row.id in parents]
            observations = worker_panes.observe_parents(
                owners, record, selection=worker_panes.SelectedWorkerRows(selection.ids)) if owners else {}
        workers = {state.row.id: state for states in observations.values() for state in states}
        active_workers = [state.row for state in workers.values()
                          if not isinstance(state, (worker_panes.ClosedWorker, worker_panes.StoppedWorker))]
        candidates_rows = [row for row in rows if row.id not in workers]
        if not isinstance(account, AllRestartAccounts):
            candidates_rows.extend(active_workers)
        reading = _read_live_members(candidates_rows, record, resolver=resolver, codex=codex, account=account)
        complete = isinstance(reading, CompleteLiveRows) and not any(
            isinstance(state, (UncertainWorker, ProcessUnknownWorker))
            for state in workers.values())
        if (report := _BULK_REPORT.get()) is not None:
            for state in workers.values():
                if isinstance(state, (UncertainWorker, ProcessUnknownWorker)) and state.row.id not in report.issues:
                    report.track(state.row, record)
                    report.failure(state.row.id, state.row.name, state.reason)
        selected = reading.rows
        if isinstance(account, AllRestartAccounts):
            selected.extend(active_workers)
        positions = {row.id: index for index, row in enumerate(record.rows)}
        selected.sort(key=lambda row: positions[row.id])
    report = _BULK_REPORT.get()
    reboot = report is not None and report.reboot
    if save_tabs and (selected or not reboot):
        try:
            if reboot:
                client.record_layout()
            else:
                client.save_layout()
        except (OSError, ValueError, herdr_types.HerdrFailure, subprocess.SubprocessError) as error:
            print(f"restart: layout anomaly: {str(error).splitlines()[0]}", file=sys.stderr)
            if reboot:
                _discovery_anomaly(f"Could not save the Herdr layout. {_error_reason(error)}")
        for row, pane in row_panes(selected, client):
            try:
                client.remember_row_tab(str(row.id), pane)
            except (OSError, ValueError, herdr_types.HerdrFailure, subprocess.SubprocessError) as error:
                _anomaly(row, error)
    return CompleteLiveRows(selected) if complete else PartialLiveRows(selected)


def prune_live_rows(reading: CompleteLiveRows | PartialLiveRows, *,
                    command: BulkCommand | None = None) -> None:
    """Prune recorded live rows after a complete read and a locked liveness check."""
    if isinstance(reading, PartialLiveRows):
        print("restart: partial liveness read; existing restart rows retained", file=sys.stderr)
        return
    saved = set(shutdown_record.read())
    for row in reading.rows:
        if row.kind == "worker" or str(row.id) not in saved:
            continue
        try:
            with staff.row_operation(str(row.id)):
                current = staff.initial_snapshot().by_id(row.id)
                if current is None:
                    continue
                observed = herdr.read_conversation(str(current.current_session))
                if isinstance(observed, herdr.LiveConversation):
                    shutdown_record.remove_started([str(row.id)])
                elif isinstance(observed, herdr.UnknownConversation):
                    _anomaly(row, observed.reason)
        except shutdown_record.RecordBusy as error:
            _report_record_busy(row.id, row.name, error, (command or BulkCommand("held-sessions")).retry())
        except (OSError, ValueError, staff.StaffRefused) as error:
            _anomaly(row, error)


def _after_unconfirmed_stop(row: staff.StaffRow, before: herdr.CodexConversations
                            ) -> herdr.LiveConversation | herdr.StoppedConversation | herdr.UnknownConversation:
    identity = str(row.current_session)
    kind, _environment, _arguments = session_launch(identity, row.name)
    if kind == "claude":
        return herdr.read_conversation(identity, kind)
    pids = before.live.get(identity, ())
    if not pids:
        return herdr.UnknownConversation(f"{identity}: the unconfirmed stop has no target pid")
    live: list[int] = []
    unknown: list[str] = []
    for pid in pids:
        try:
            place = herdr.herdr_place_of(pid)
            if isinstance(place, herdr.HerdrPlace):
                herdr.forget_pane_evidence(place.session, place.pane)
            process = herdr.native_process(pid, kind)
            if process.current_identity != identity:
                raise ValueError(f"pid {pid}: current conversation differs; original generation is unknown")
            state = sessions.process_liveness(pid, process.started)
            if isinstance(state, sessions.LiveSessionProcess):
                live.append(pid)
            elif isinstance(state, sessions.SessionLivenessUnknown):
                raise OSError(state.reason)
            else:
                place = herdr.herdr_place_of(pid)
                if isinstance(place, herdr.HerdrPlace):
                    herdr.forget_pane_evidence(place.session, place.pane)
                current = herdr.native_process(pid, kind)
                if current.started != process.started and current.current_identity != identity:
                    continue
                raise ValueError(f"pid {pid}: generation changed without proof that the target exited")
        except (OSError, ValueError, herdr_types.HerdrFailure) as error:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            except OSError:
                pass
            unknown.append(str(error))
    if live:
        return herdr.LiveConversation("codex", tuple(live))
    if unknown:
        return herdr.UnknownConversation(unknown[0])
    return herdr.StoppedConversation()


class AfterStop(Enum):
    RESTART = "resume the selected rows"
    KEEP_STOPPED = "keep the selected rows for the next restart"


def _detach_stop(command: BulkCommand, selected: list[staff.StaffRow], record: staff.StaffSnapshot) -> int:
    report = _BULK_REPORT.get()
    if report is None:
        return run_command(lambda: _detach_stop(command, selected, record), command)
    for row in selected:
        report.track(row, record)

    stream = report.log_stream
    if stream is None:
        step = report.log_repair_step()
        detail = _error_reason(report.log_error) if report.log_error is not None else "The log is unavailable"
        reason = f"Could not write the log. {detail}"
        for key, issue in report.issues.items():
            if issue.operator and issue.step == f"Nate runs {report.retry} again.":
                report.issues[key] = replace(issue, step=step)
    else:
        launch_error: Exception | None = None
        try:
            arguments = replace(command, only=tuple(str(row.id) for row in selected)).arguments()
            _ = stop_resume.detach([*arguments, "--parent-report", report.detach_data()], "restart",
                                   program=Path(__file__).resolve(), log=report.log, log_fd=stream.fileno())
        except Exception as error:
            launch_error = error
        try:
            claimed = _claim_log(report.log)
        except Exception as error:
            owner = _log_claim_pid(report.log)
            if owner is not None and owner != os.getpid():
                report.writer = LogWriter.CHILD
                return 0
            detail = f"Could not claim the log. {_error_reason(error)}"
            if owner == os.getpid():
                reason = f"The launcher could not start the detached command. {detail}"
                step = _plain_terminal_step(report.retry)
            else:
                report.writer = LogWriter.UNKNOWN
                reason = detail
                step = "Nate sends this error to the efficiency unit."
        else:
            if not claimed:
                report.writer = LogWriter.CHILD
                return 0
            detail = (_launch_failure_reason(launch_error) if launch_error is not None else
                      "The detached process did not claim the log")
            reason = f"The launcher could not start the detached command. {detail}"
            step = _plain_terminal_step(report.retry)

    for row in selected:
        report.issues[row.id] = FollowUp(report.targets[row.id], reason, step, operator=True)
    return 2


def stop_all(*, dry_run: bool = False, only: tuple[str, ...] = (),
             account: AllRestartAccounts | accounts.AccountSelection = ALL_RESTART_ACCOUNTS) -> int:
    return restart(stop_all=True, dry_run=dry_run, only=only, account=account, after_stop=AfterStop.KEEP_STOPPED)


def restart(*, stop_all: bool = False, dry_run: bool = False, only: tuple[str, ...] = (),
            account: AllRestartAccounts | accounts.AccountSelection = ALL_RESTART_ACCOUNTS,
            after_stop: AfterStop = AfterStop.RESTART,
            why: str = DEFAULT_RESTART_CAUSE) -> int:
    activity = (nullcontext() if dry_run else shutdown_record.active_operation(
        record_lock_seconds=shutdown_record.record_lock_seconds()))
    with activity, ExitStack() as pane_evidence:
        _ = pane_evidence.enter_context(herdr.pane_evidence_run())
        return _restart(pane_evidence, stop_all=stop_all, dry_run=dry_run, only=only, account=account,
                        after_stop=after_stop, why=why)


def _restart(pane_evidence: ExitStack, *, stop_all: bool, dry_run: bool, only: tuple[str, ...],
             account: AllRestartAccounts | accounts.AccountSelection, after_stop: AfterStop, why: str) -> int:
    report = _BULK_REPORT.get()
    command = BulkCommand("stop" if after_stop is AfterStop.KEEP_STOPPED else "restart",
                          account=None if isinstance(account, AllRestartAccounts) else account.label,
                          all_rows=stop_all, only=only, why=why, dry_run=dry_run)
    retry = report.retry if report is not None else command.retry()

    def stop_requested() -> bool:
        return report is not None and report.stop_signal is not None

    if report is not None and report.stop_signal is not None:
        report.mark_not_started(report.targets, report.retry)
        return 0
    upkeep_blocked: dict[staff.StaffId, tuple[staff.StaffRow, staff.UpkeepRowBusy]] = {}
    try:
        record = staff.initial_snapshot()
        if stop_all and not dry_run:
            deadline = time.monotonic() + conf.read("shutdown").number("UPKEEP_ROW_WAIT_SECONDS")
            only = tuple(str(row.id) if (row := record.by_member_reference(reference)) is not None else reference
                         for reference in only)
            for row in record.find(kind="producer"):
                if only and row.id not in only:
                    continue
                try:
                    staff.wait_for_upkeep_row(row, deadline)
                except staff.UpkeepRowBusy as error:
                    upkeep_blocked[row.id] = row, error
            record = staff.initial_snapshot()
    except (OSError, ValueError, staff.StaffRefused) as error:
        if _DETACHED_CHILD.get():
            raise OSError(f"Could not read the staff record. {_error_reason(error)}") from error
        _discovery_anomaly(f"Could not prepare staff discovery: {_error_reason(error)}")
        return 0
    selection = (SelectedRestartRows(frozenset(row.id for reference in only
        if (row := record.by_member_reference(reference)) is not None)) if only else AllRestartRows.CURRENT)
    preview = report.preview.append if report is not None else print
    with herdr.observation_snapshot():
        resolver = session_staff.RowResolver(record)
        client = herdr.Herdr(herdr.shared_session())
        observed = _observations(record, client)
        selected: list[staff.StaffRow] = []
        reading: CompleteLiveRows | PartialLiveRows = CompleteLiveRows([])
        if stop_all:
            reading = live_rows(record, client, save_tabs=not dry_run, resolver=resolver, account=account,
                                observed=observed, selection=selection)
            if isinstance(reading, PartialLiveRows) and reading.registry_discovery_failed:
                return 1
            selected = [row for row in reading.rows if row.id not in upkeep_blocked]
            if upkeep_blocked:
                reading = PartialLiveRows(selected)
                for identity, (before, error) in upkeep_blocked.items():
                    row = record.by_id(identity) or before
                    if report is not None:
                        report.track(row, record)
                    _anomaly(row, error, retry=retry)
        select_only = bool(only) and not stop_all
        pending_ids = ([] if after_stop == AfterStop.KEEP_STOPPED else
                       list(only) if select_only else
                       [] if stop_all and (only or not isinstance(account, AllRestartAccounts)) else shutdown_record.read())
        if dry_run and not select_only and not stop_all and isinstance(account, AllRestartAccounts):
            if report is None:
                path = shutdown_record.record_path()
                saved_at = time.strftime("%H:%M", time.localtime(path.stat().st_mtime)) if path.exists() else "unknown"
                print(f"restart record: {len(pending_ids)} rows saved at {saved_at}")
            shown = live_rows(record, client, resolver=resolver, observed=observed)
            if isinstance(shown, PartialLiveRows) and shown.registry_discovery_failed:
                return 1
            for row in shown.rows:
                if str(row.id) not in pending_ids:
                    preview(f"{row.name}: live, not in the restart record")
        saved_ids: set[str] | None = None
        for reference in pending_ids:
            identity = staff.StaffId(reference)
            row = record.rows_by_id.get(identity)
            retired = row is None and record.by_id(identity) is not None
            if select_only and row is None and not retired:
                if saved_ids is None:
                    saved_ids = set(shutdown_record.read())
                # A saved ID takes precedence over a name or conversation reference.
                if reference not in saved_ids:
                    row = record.by_current_session(reference)
                    if row is None:
                        matches = [item for item in record.rows if item.name == reference]
                        row = matches[0] if len(matches) == 1 else None
                    if row is None:
                        if report is not None:
                            report.issues[reference] = FollowUp(reference, "The staff row is unknown or ambiguous",
                                "Nate selects a staff row ID.", operator=True)
                        print(f"{reference}: unknown or ambiguous staff row", file=sys.stderr)
                        continue
            if row is None:
                reason = "the producer is retired" if retired else "no staff row"
            else:
                reason = None if select_only else leaves_record(row, record, runs=observed.runs)
            if row is None or reason is not None:
                name = reference if row is None else row.name
                if not dry_run:
                    try:
                        shutdown_record.forget([reference])
                    except shutdown_record.RecordBusy as error:
                        _report_record_busy(reference, name, error, retry)
                        continue
                if report is not None:
                    report.issues[reference] = FollowUp(name, reason or "No staff row",
                        "The restart entry can be removed." if dry_run else "Restart removed the entry.")
                print(f"{' '.join(name.split())}: restart anomaly: {reason}; "
                      + ("would leave the restart record" if dry_run else "removed from the restart record"), file=sys.stderr)
                continue
            if not isinstance(account, AllRestartAccounts):
                try:
                    kind, environment, _arguments = session_launch(str(row.current_session), row.name)
                    if not account_matches(kind, environment, account):
                        continue
                except (OSError, ValueError) as error:
                    _anomaly(row, error)
                    continue
            if row not in selected and row.id not in upkeep_blocked:
                selected.append(row)
        if dry_run:
            positions = {tab["staff_id"]: f"workspace {space['name']}, tab {index} ({record.rows_by_id[staff.StaffId(tab['staff_id'])].name})"
                         for space in herdr.saved_layout(client.session)["workspaces"]
                         for index, tab in enumerate(space["tabs"], 1) if staff.StaffId(tab["staff_id"]) in record.rows_by_id}
            visible = row_panes(list(record.rows) if stop_all and not only and isinstance(account, AllRestartAccounts) else selected, client)
            for row, pane in visible:
                if row not in selected:
                    continue
                try:
                    workspace, index, position = client.pane_tab(pane)
                    del position
                    positions[str(row.id)] = f"workspace {workspace}, tab {index} ({row.name})"
                except (OSError, ValueError, herdr_types.HerdrFailure) as error:
                    _anomaly(row, error)
            for row in selected:
                if report is not None:
                    report.track(row, record)
                action = "; would stop and save" if after_stop == AfterStop.KEEP_STOPPED else ""
                preview(f"{row.name}: {row.id}, {row.kind}, {positions.get(str(row.id), 'no tab')}{action}")
            for row in {row.id: row for row, _pane in visible if row not in selected}.values():
                preview(f"{row.name}: left alone; no proved local restart target")
            return 0
    if report is not None:
        for row in selected:
            report.track(row, record)
        if report.reboot and stop_all and (isinstance(reading, PartialLiveRows) or report.issues):
            return 1
        if report.fixed_targets is not None:
            missing = report.fixed_targets.difference(row.id for row in selected) - report.issues.keys()
            for key in missing:
                report.failure(key, report.targets[key], "The current live session list does not include this session")
    errors = (OSError, ValueError, staff.StaffRefused, herdr_types.HerdrFailure, subprocess.SubprocessError)
    started: list[str] = []
    worker_scope = (WorkerRestartScope.SELECTED_ROWS if only or not isinstance(account, AllRestartAccounts)
                    else WorkerRestartScope.FOLLOW_OWNER)
    exit_code = 0
    holders: dict[staff.StaffId, staff.StaffRow] = {}
    members_by_holder: dict[staff.StaffId, list[str]] = {}
    selected_ids = {row.id for row in selected}
    for row in selected:
        parent = record.parent_of(row) if row.kind == "worker" else None
        holder = (parent if worker_scope is WorkerRestartScope.FOLLOW_OWNER
                  and parent is not None and parent.id in selected_ids else row)
        holders[holder.id] = holder
        members_by_holder.setdefault(holder.id, []).append(str(row.id))

    resume_command = replace(command, verb="restart", all_rows=False)

    def resume_retry(members: Iterable[str]) -> str:
        return replace(resume_command, only=tuple(members)).retry()

    def defer_resume(identity: staff.StaffId) -> None:
        if report is not None:
            members = members_by_holder[identity]
            report.mark_not_started(members, resume_retry(members))

    def record_busy(holder: staff.StaffRow, error: shutdown_record.RecordBusy, *, resuming: bool = False) -> None:
        for member in members_by_holder[holder.id]:
            step_command = resume_retry([member]) if resuming else retry
            _report_record_busy(member, record.rows_by_id[staff.StaffId(member)].name, error, step_command)

    # Reboot saves every selected row before it can stop the server.
    leaving = {str(row.id) for row in selected if stop_all and not (report is not None and report.reboot)
               and leaves_record(row, record, runs=observed.runs) is not None}
    saved_by_holder = {holder: [member for member in members if member not in leaving]
                       for holder, members in members_by_holder.items()}
    callers = {os.environ.get("CLAUDE_CODE_SESSION_ID"), os.environ.get("CODEX_THREAD_ID")} - {None, ""}
    if stop_all and any(row.current_session in callers for row in selected):
        if report is not None and report.reboot:
            _discovery_anomaly("The stop includes the session that runs this reboot")
            return 1
        return _detach_stop(command, selected, record)
    lock_wait = conf.read("shutdown").number("UPKEEP_ROW_WAIT_SECONDS") if holders else 0
    from scripts.herdr.worker_panes import (
        CheckedRolePolicy, StartRefused, checked_role_policy, dispatch_role, use_checked_resume_policies,
    )

    worker_policies: dict[staff.StaffId, CheckedRolePolicy] = {}
    refused_workers: set[staff.StaffId] = set()

    def omit_refused_workers(refused: set[staff.StaffId]) -> None:
        for identity in refused:
            _ = holders.pop(identity, None)
        for holder in holders:
            members_by_holder[holder] = [member for member in members_by_holder[holder] if staff.StaffId(member) not in refused]
            saved_by_holder[holder] = [member for member in saved_by_holder[holder] if staff.StaffId(member) not in refused]

    if stop_all and after_stop is AfterStop.RESTART:
        for row in selected:
            if row.kind != "worker":
                continue
            policy = checked_role_policy(dispatch_role(row))
            if isinstance(policy, StartRefused):
                _anomaly(row, f"{policy.reason.value}: {policy.detail}")
                refused_workers.add(row.id)
            else:
                worker_policies[row.id] = policy
        omit_refused_workers(refused_workers)

    if report is not None and _DETACHED_CHILD.get() and stop_all:
        _record_stop_plan(report, (row for row in selected if row.id not in refused_workers))
    if stop_all and after_stop is AfterStop.KEEP_STOPPED and not only and isinstance(account, AllRestartAccounts):
        prune_live_rows(reading, command=replace(command, verb="held-sessions", all_rows=False))

    pre_stop_codex = observed.codex
    keep_running = (frozenset(row.id for row in record.rows if row.kind == "worker" and row.id not in worker_policies)
                    if stop_all and after_stop is AfterStop.RESTART else frozenset[staff.StaffId]())
    unconfirmed: set[staff.StaffId] = set()
    unconfirmed_workers: set[staff.StaffId] = set()
    role_changes_by_holder: dict[staff.StaffId, set[staff.StaffId]] = {}

    def refuse_changed_worker_roles(members: list[str]) -> set[staff.StaffId]:
        checked = {identity: worker_policies[identity] for member in members
                   if (identity := staff.StaffId(member)) in worker_policies}
        changed: set[staff.StaffId] = set()
        if checked:
            current_record = staff.initial_snapshot()
            for identity, policy in checked.items():
                row = current_record.by_id(identity)
                if row is None or row.kind != "worker" or row.role != staff.KnownWorkerRole(policy.role):
                    changed.add(identity)
                    role = row.role.value if row is not None and isinstance(row.role, staff.KnownWorkerRole) else "unavailable"
                    _anomaly(row or record.rows_by_id[identity],
                             f"worker role changed from {policy.role} to {role}; worker kept running")
        return changed

    def stop_holder(holder: staff.StaffRow, previous: Event, recorded: Event) -> tuple[bool, tuple[staff.StaffId, ...]] | None:
        try:
            _ = previous.wait()
            if stop_requested():
                return None
            with ExitStack() as locks, staff.upkeep_lock_wait(lock_wait):
                _ = locks.enter_context(staff.row_operation(str(holder.id)))
                for member in members_by_holder[holder.id]:
                    if member != str(holder.id):
                        _ = locks.enter_context(staff.row_operation(member))
                changed = refuse_changed_worker_roles(members_by_holder[holder.id])
                if stop_requested():
                    return None
                if changed:
                    role_changes_by_holder[holder.id] = changed
                if holder.id in changed:
                    return True, ()
                shutdown_record.update(started=[], pending=[member for member in saved_by_holder[holder.id]
                                        if staff.StaffId(member) not in changed])
                recorded.set()
                previous_hold: staff.Paused | staff.Active | None = None
                if after_stop is AfterStop.KEEP_STOPPED and holder.kind == "producer":
                    try:
                        current = staff.initial_snapshot().rows_by_id.get(holder.id)
                        if current is None:
                            raise staff.StaffNotFound(holder.id)
                        if (current.kind != "producer" or current.current_session != holder.current_session
                                or current.worktree != holder.worktree):
                            raise ValueError("the producer changed before the stop")
                        if not isinstance(current.exited, staff.Exited):
                            _ = staff.exit(str(holder.id), "agents stop --all ran")
                            previous_hold = current.paused
                    except (OSError, ValueError, staff.StaffRefused, staff.StaffNotFound) as error:
                        _anomaly(holder, f"Could not save the exit hold. {_error_reason(error)}")
                        shutdown_record.remove_started([str(holder.id)])
                        return False, ()
                workers: tuple[staff.StaffId, ...] = ()
                if worker_scope is WorkerRestartScope.FOLLOW_OWNER and holder.kind in ("unit", "showrunner"):
                    failures: dict[staff.StaffId, str] = {}
                    unresolved = stop_resume.stop_unit_workers(holder.id, record, codex=observed.codex,
                                                              keep_running=keep_running.union(changed), failures=failures)
                    if unresolved and holder.id in observed.pane_owners:
                        workers = tuple(child.id for child in record.find(kind="worker", parent=holder.id)
                                        if child.id in selected_ids and (not failures or child.id in failures))
                        if report is not None and after_stop is AfterStop.KEEP_STOPPED:
                            for identity in workers:
                                row = record.rows_by_id[identity]
                                if identity not in report.issues and identity in failures:
                                    report.failure(identity, row.name, failures[identity])
                stopped = stop_row(holder, client, codex=observed.codex)
                if not stopped and holder.kind != "worker":
                    if previous_hold is not None:
                        _ = staff.set_hold(str(holder.id), previous_hold)
                    shutdown_record.remove_started([str(holder.id)])
                return stopped, workers
        finally:
            recorded.set()

    stop_failed = False
    if stop_all and holders:
        recorded = [Event() for _ in range(len(holders) + 1)]
        recorded[0].set()
        stopping = holders.copy()
        stop_jobs = {holder.id: partial(stop_holder, holder, recorded[index], recorded[index + 1])
                     for index, holder in enumerate(stopping.values())}

        def record_stop(identity: staff.StaffId, future: Future[tuple[bool, tuple[staff.StaffId, ...]] | None]) -> None:
            nonlocal stop_failed
            holder = stopping[identity]
            try:
                result = future.result()
                if result is None:
                    if report is not None:
                        report.mark_not_started(members_by_holder[identity], report.retry)
                    return
                stopped, workers = result
                unconfirmed_workers.update(workers)
                if not stopped:
                    unconfirmed.add(identity)
                if report is not None and after_stop is AfterStop.KEEP_STOPPED:
                    unresolved = set(workers)
                    if not stopped:
                        unresolved.add(identity)
                    for member in members_by_holder[identity]:
                        if staff.StaffId(member) not in unresolved:
                            report.finish(member)
            except shutdown_record.RecordBusy as error:
                _ = holders.pop(identity, None)
                record_busy(holder, error)
            except staff.StaffRefused as error:
                _ = holders.pop(identity, None)
                _anomaly(holder, error, retry=retry)
                if report is not None:
                    for member in members_by_holder[identity]:
                        if member != identity:
                            report.failure(member, report.targets[member], f"Could not lock all rows in {holder.name}")
            except errors as error:
                unconfirmed.add(identity)
                if identity in observed.pane_owners:
                    unconfirmed_workers.update(child.id for child in record.find(kind="worker", parent=identity)
                                               if child.id in selected_ids)
                _anomaly(holder, error)
            except BaseException as error:
                if report is None or not _DETACHED_CHILD.get():
                    raise
                report.detached_row_failure(identity, error)
                stop_failed = True

        _collect_restart_results(stop_jobs, record_stop)
    defer_after_stop = after_stop is AfterStop.RESTART and stop_requested()
    if defer_after_stop:
        for identity in holders:
            defer_resume(identity)
    if stop_failed:
        raise DetachedRowsFailed("One or more session stops failed")
    if after_stop is AfterStop.KEEP_STOPPED or defer_after_stop:
        return 0
    changed = {identity for identities in role_changes_by_holder.values() for identity in identities}
    if changed:
        omit_refused_workers(changed)
        refused_workers.update(changed)
    selected_sessions = ({str(row.current_session) for row in selected if row.id not in refused_workers}
                         if stop_all else set[str]())
    blocked_workers = refused_workers.copy()
    for child in record.rows:
        if child.id not in unconfirmed_workers or child.id in refused_workers:
            continue
        try:
            fresh = _after_unconfirmed_stop(child, pre_stop_codex)
        except errors as error:
            fresh = herdr.UnknownConversation(str(error))
        if not isinstance(fresh, herdr.StoppedConversation):
            blocked_workers.add(child.id)
            selected_sessions.discard(str(child.current_session))
            _anomaly(child, "target process still runs; worker kept pending" if isinstance(fresh, herdr.LiveConversation) else fresh.reason)
    observed = replace(observed, codex=herdr.CodexConversations(
        {identity: pids for identity, pids in observed.codex.live.items() if identity not in selected_sessions},
        observed.codex.unknown))
    present = False
    if holders or (report is not None and report.reboot and not stop_all):
        try:
            server = herdr.restore_server(client.session, int(herdr.PROGRAM_TIMEOUT_SECONDS * 1000),
                                     session_kind=herdr.security_session())
            present = isinstance(server, herdr.WindowServerCreated)
        except errors as error:
            _discovery_anomaly(f"Could not restore the server: {_error_reason(error)}")
        if stop_all:
            # A stop read can still show a worker that has exited. Read pane evidence again for restore.
            pane_evidence.close()
            _ = pane_evidence.enter_context(herdr.pane_evidence_run())
    resume_checks = DirectorResumeChecks(worker_restores=WorkerRestoreReports(command=resume_command))
    resume_policies = {record.rows_by_id[identity].current_session: policy
                       for identity, policy in worker_policies.items() if identity not in blocked_workers}
    record_save_failures: dict[str, OSError | ValueError] = {}

    def report_record_save_failures() -> None:
        if report is None:
            return
        for member, error in record_save_failures.items():
            if isinstance(error, shutdown_record.RecordBusy):
                reason = str(error)
                step = _record_retry_step(resume_retry([member]))
            else:
                reason = f"Could not save the restart record. {_error_reason(error)}"
                step = "Nate sends this error to the efficiency unit."
            issue = report.issues.get(member)
            report.issues[member] = (FollowUp(report.targets[member], reason, step, operator=True) if issue is None else
                replace(issue, reason=f"{issue.reason.rstrip('.')}. {reason}", step=f"{issue.step} {step}", operator=True))

    def remove_started(holder: staff.StaffRow, *, started_holder: bool) -> None:
        if (started_holder and holder.kind == "producer"
                and (isinstance(holder.exited, staff.Exited) or isinstance(holder.paused, staff.Paused))):
            _ = staff.resume(str(holder.id))
        resolved = resume_checks.worker_restores.resolved(holder.id)
        done = [member for member in members_by_holder[holder.id]
                if (member_id := staff.StaffId(member)) not in blocked_workers
                and (member_id in resolved or (started_holder and record.rows_by_id[member_id].kind != "worker"))]
        if done:
            try:
                shutdown_record.remove_started(done)
            except (OSError, ValueError) as error:
                if report is None:
                    if isinstance(error, shutdown_record.RecordBusy):
                        for member in done:
                            row = record.rows_by_id[staff.StaffId(member)]
                            _report_record_busy(member, row.name, error, resume_retry([member]))
                        return
                    raise
                record_save_failures.update(dict.fromkeys(done, error))

    def start_holder(holder: staff.StaffRow, focus: bool,
                     success_report: RestartSuccessReport = RestartSuccessReport.START_ROW) -> RestartDisposition | None:
        if stop_requested():
            return None
        with ExitStack() as locks, use_checked_resume_policies(resume_policies), staff.upkeep_lock_wait(lock_wait):
            _ = locks.enter_context(staff.row_operation(str(holder.id)))
            for member in members_by_holder[holder.id]:
                if member != str(holder.id):
                    _ = locks.enter_context(staff.row_operation(member))
            if stop_requested():
                return None
            shutdown_record.update(started=[], pending=saved_by_holder[holder.id])
            if holder.id in blocked_workers:
                return RestartDisposition.HELD
            current_record = staff.initial_snapshot()
            current = current_record.rows_by_id.get(holder.id)
            if current is None:
                gone = [member for member in members_by_holder[holder.id]
                        if staff.StaffId(member) not in current_record.rows_by_id]
                retired = current_record.by_id(holder.id) is not None
                name = str(holder.id) if retired else holder.name
                reason = "the producer is retired" if retired else "no staff row"
                try:
                    shutdown_record.forget(gone)
                except shutdown_record.RecordBusy as error:
                    _report_record_busy(holder.id, name, error, resume_retry(gone))
                    return RestartDisposition.REMOVED
                if report is not None:
                    report.issues[holder.id] = FollowUp(name, reason if retired else "The staff row is gone",
                                                       "Restart removed the entry.")
                print(f"{name}: restart anomaly: {reason}; removed from the restart record", file=sys.stderr)
                return RestartDisposition.REMOVED
            current_record = replace(current_record, rows=tuple(row for row in current_record.rows if row.id not in blocked_workers))
            if current.kind in ("unit", "showrunner") and (
                    isinstance(current.paused, staff.Paused) or isinstance(current.exited, staff.Exited)):
                if report is not None:
                    hold = "exited" if isinstance(current.exited, staff.Exited) else "paused"
                    step = "The director resumes the session when ready." if report.reboot else "The session stays stopped."
                    for member in members_by_holder[holder.id]:
                        report.issues[member] = FollowUp(report.targets[member], f"{current.name} is {hold}", step)
                print(f"{current.name}: {'exited' if isinstance(current.exited, staff.Exited) else 'paused'}")
                return RestartDisposition.HELD
            if holder.id in unconfirmed:
                fresh = _after_unconfirmed_stop(current, pre_stop_codex)
                if isinstance(fresh, herdr.UnknownConversation):
                    raise OSError(fresh.reason)
                if isinstance(fresh, herdr.LiveConversation):
                    if report is not None and current.kind != "worker":
                        report.finish(current.id)
                    try:
                        if current.kind == "worker" or worker_scope is WorkerRestartScope.FOLLOW_OWNER:
                            _ = _restore_workers(current, current_record, observed=observed,
                                                 reports=resume_checks.worker_restores)
                    except WorkerRestoreIncomplete:
                        remove_started(holder, started_holder=False)
                        raise
                    print(f"{current.name}: unchanged")
                    remove_started(current, started_holder=True)
                    return RestartDisposition.RESTORED
            facts = session_staff.RowResolver(current_record)
            opening = (herdr.OpeningPrompt(resume_message.resume_message(why, workers=resume_message.worker_mode()))
                       if current.kind in ("unit", "showrunner") else NO_RESTART_MESSAGE)
            launch_row = (start_row if success_report is RestartSuccessReport.START_ROW else
                          partial(start_row, success_report=success_report))
            if current.kind == "worker":
                launch_row = partial(launch_row, live_worker_recovery=LiveWorkerRecovery.RESTORE_BEFORE_REMOVAL)
            try:
                if worker_scope is WorkerRestartScope.FOLLOW_OWNER:
                    pane = launch_row(current, current_record, client, present=focus, resolver=facts, observed=observed,
                                      opening=opening, resume_checks=resume_checks)
                else:
                    pane = launch_row(current, current_record, client, present=focus, resolver=facts,
                                      target_account="" if isinstance(account, AllRestartAccounts) else account,
                                      worker_scope=worker_scope, observed=observed, opening=opening, resume_checks=resume_checks)
            except WorkerRestoreIncomplete:
                remove_started(holder, started_holder=False)
                raise
            remove_started(current, started_holder=True)
            return RestartDisposition.STARTED if pane else RestartDisposition.RESTORED

    restarted: set[Path] = set()

    def record_start(holder: staff.StaffRow, disposition: RestartDisposition) -> None:
        members = members_by_holder[holder.id]
        if disposition in (RestartDisposition.HELD, RestartDisposition.REMOVED):
            return
        if report is not None and holder.kind != "worker":
            report.finish(holder.id)
        started.extend(member for member in members if staff.StaffId(member) not in blocked_workers)
        if disposition is RestartDisposition.STARTED and isinstance(holder.worktree, staff.PresentWorktree):
            restarted.add(holder.worktree.path)

    failed_starts: list[FailedHolderStart] = []
    start_failed = False
    if holders:
        focused = next(iter(holders)) if present else None
        start_jobs = {holder.id: partial(start_holder, holder, holder.id == focused) for holder in holders.values()}

        def finish_start(identity: staff.StaffId, future: Future[RestartDisposition | None]) -> None:
            nonlocal exit_code, start_failed
            holder = holders[identity]
            try:
                disposition = future.result()
                if disposition is None:
                    defer_resume(identity)
                else:
                    record_start(holder, disposition)
            except ClaudeTrustWriteFailed:
                exit_code = 1
                if report is not None:
                    report.failure(identity, holder.name, "Could not write the account trust record")
            except shutdown_record.RecordBusy as error:
                record_busy(holder, error, resuming=True)
            except staff.StaffRefused as error:
                _anomaly(holder, error)
            except errors as error:
                failed_starts.append(FailedHolderStart(holder, identity == focused, error))
            except BaseException as error:
                if report is None or not _DETACHED_CHILD.get():
                    raise
                report.detached_row_failure(identity, error)
                start_failed = True

        _collect_restart_results(start_jobs, finish_start)
    if start_failed:
        resume_checks.worker_restores.report()
        for failed in failed_starts:
            if not isinstance(failed.error, WorkerRestoreIncomplete):
                _anomaly(failed.holder, failed.error)
        report_record_save_failures()
        raise DetachedRowsFailed("One or more session resumes failed")
    for failed in failed_starts:
        holder = failed.holder
        fresh: herdr.LiveConversation | herdr.StoppedConversation | herdr.UnknownConversation
        fresh = herdr.UnknownConversation(str(failed.error))
        if not stop_requested() and holder.kind in ("unit", "showrunner"):
            try:
                fresh = herdr.read_conversation(str(holder.current_session))
            except errors:
                pass
        if not isinstance(fresh, herdr.StoppedConversation):
            if not isinstance(failed.error, WorkerRestoreIncomplete):
                _anomaly(holder, failed.error)
            continue
        try:
            disposition = start_holder(holder, failed.focus, RestartSuccessReport.CALLER)
            if disposition is None:
                if not isinstance(failed.error, WorkerRestoreIncomplete):
                    _anomaly(holder, failed.error)
                continue
            record_start(holder, disposition)
            if disposition is RestartDisposition.STARTED:
                print(f"{holder.name}: down; started")
        except ClaudeTrustWriteFailed:
            exit_code = 1
            if report is not None:
                report.failure(holder.id, holder.name, "Could not write the account trust record")
        except WorkerRestoreIncomplete:
            continue
        except shutdown_record.RecordBusy as error:
            record_busy(holder, error, resuming=True)
        except staff.StaffRefused as error:
            _anomaly(holder, error)
        except errors as error:
            if report is not None:
                report.failure(holder.id, holder.name, _error_reason(error))
            else:
                print(f"{holder.name}: down: {_error_reason(error)}")
    if started:
        try:
            _ = client.sort_tabs()
        except errors as error:
            print(f"restart: layout anomaly: {str(error).splitlines()[0]}", file=sys.stderr)
    if restarted:
        from scripts.worktree import worktrees

        try:
            scope = worktrees.RestartedWorktrees(frozenset(path.resolve() for path in restarted))
            for link in worktrees.old_worktree_links(remove_unused=True, scope=scope):
                if link.removed:
                    print(f"restart: removed old worktree link {link.old}")
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, worktrees.Refused) as error:
            print(f"restart: kept old worktree links: {_error_reason(error)}", file=sys.stderr)
    resume_checks.report()
    report_record_save_failures()
    return exit_code


def _notify_log_claim(ready_fd: int | None, *, systemd: bool) -> None:
    if ready_fd is not None:
        try:
            with suppress(BrokenPipeError):
                _ = os.write(ready_fd, b"1")
        finally:
            os.close(ready_fd)
    if systemd:
        address = os.environ.pop("NOTIFY_SOCKET", "")
        if not address:
            raise OSError("The systemd readiness socket is unavailable because NOTIFY_SOCKET is missing")
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notifier:
            notifier.connect("\0" + address[1:] if address.startswith("@") else address)
            _ = notifier.send(b"READY=1")


def _save_detached_report(log: io.FileIO, result: str) -> None:
    _write_log(log, result)
    os.fsync(log.fileno())


def _run_detached_child(action: Callable[[], int], report: BulkReport, log_fd: int, *, dry_run: bool) -> int:
    claimed = False
    result_offset = None
    output = io.StringIO()
    stage = "Could not claim the log"
    step = _plain_terminal_step(report.retry)
    handlers = ExitStack()
    report_committed = False

    def request_stop(number: int, _frame: object) -> None:
        if not report_committed and report.stop_signal is None:
            report.stop_signal = signal.Signals(number)

    def report_text(failure: FollowUp | None, prefix: str) -> str:
        return prefix + "\n".join(report.lines(dry_run=dry_run, failure=failure)) + "\n"

    def save_report(log: io.FileIO, failure: FollowUp | None, *, prefix: str = "") -> None:
        nonlocal report_committed
        received = report.stop_signal
        offset = log.tell()
        _save_detached_report(log, report_text(failure, prefix))
        report_committed = True
        # Include a signal that arrived during the first sync.
        if report.stop_signal != received:
            _ = log.truncate(offset)
            _ = log.seek(offset)
            _save_detached_report(log, report_text(failure, prefix))

    # Update test_detached_child_reports_each_fault_after_the_claim when you change these steps.
    try:
        for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT, signal.SIGQUIT):
            _ = handlers.callback(signal.signal, number, signal.getsignal(number))
            _ = signal.signal(number, request_stop)
        claimed = _claim_log(report.log)
        if not claimed:
            return 0

        stage = f"Could not write the log at {report.log}. The command did not start"
        with io.FileIO(log_fd, "wb", closefd=False) as log:
            _ = log.seek(0, os.SEEK_END)
            report.log_stream = log
            stage = "Could not complete the detached command"
            token = _BULK_REPORT.set(report)
            child_token = _DETACHED_CHILD.set(True)
            try:
                with redirect_stdout(output), redirect_stderr(output):
                    code = action()
            finally:
                _DETACHED_CHILD.reset(child_token)
                _BULK_REPORT.reset(token)
            failure = None
            if code:
                detail = _error_reason(output.getvalue().strip() or stage).removeprefix("shutdown: ")
                failure = report.issues.get("") or FollowUp(report.command, detail,
                    "Nate sends this error to the efficiency unit.", operator=True)
            stage = "Could not write the report"
            result_offset = log.tell()
            save_report(log, failure)
        return code
    except BaseException as error:
        detail = _error_reason(error)
        if isinstance(error, SystemExit):
            lines = output.getvalue().strip().splitlines()
            detail = lines[-1] if lines else detail
        if not claimed and _log_claim_pid(report.log) != os.getpid():
            print(f"{stage} at {report.log}. {detail}", file=sys.stderr, flush=True)
            return 2
        prefix = ""
        if isinstance(error, StopPlanWriteFailed):
            stage = f"Could not write the log at {report.log}. The command did not start"
            prefix = "\n"
        if report.stop_plan_recorded or result_offset is not None:
            step = "Nate sends this error to the efficiency unit."
        failure = FollowUp(report.command, f"{stage}. {detail}", step, operator=True)
        try:
            with io.FileIO(log_fd, "wb", closefd=False) as log:
                if result_offset is not None:
                    _ = log.truncate(result_offset)
                _ = log.seek(0, os.SEEK_END)
                save_report(log, failure, prefix=prefix)
        except BaseException as log_error:
            report_committed = True
            print(f"Could not finish writing the log at {report.log}. {_error_reason(log_error)}",
                  file=sys.stderr, flush=True)
            print(report_text(failure, prefix), end="", file=sys.stderr, flush=True)
        return 2
    finally:
        handlers.close()


class DetachedCommandOptions(argparse.Namespace):
    account: str | None = None
    all: bool = False
    only: str = ""
    why: str = DEFAULT_RESTART_CAUSE
    dry_run: bool = False
    here: bool = False


def main() -> int:
    """Run detached restart work and save its output."""
    if __name__ == "__main__":
        sys.modules["scripts.agents.restart"] = sys.modules[__name__]
    parser = argparse.ArgumentParser(add_help=False)
    _ = parser.add_argument("--log", type=Path, required=True)
    _ = parser.add_argument("--log-fd", type=int, required=True)
    _ = parser.add_argument("--ready-fd", type=int)
    _ = parser.add_argument("--notify-systemd", action="store_true")
    _ = parser.add_argument("--requester", default="")
    _ = parser.add_argument("--parent-report", type=json.loads)
    options, arguments = parser.parse_known_args()
    log = cast(Path, options.log)
    data = cast(DetachedReport | None, options.parent_report)
    stop = arguments[:1] == ["stop"]
    title = "agents stop --all" if stop else "agents restart"
    if data is not None:
        report = BulkReport(log, title, data["retry"])
        report.receive(data)
    else:
        selection = argparse.ArgumentParser(add_help=False)
        _ = selection.add_argument("--all", action="store_true")
        _ = selection.add_argument("--only", required=True)
        _ = selection.add_argument("--dry-run", action="store_true")
        if stop:
            _ = selection.add_argument("--account")
        else:
            _ = selection.add_argument("account", nargs="?")
            _ = selection.add_argument("--why", default=DEFAULT_RESTART_CAUSE)
            _ = selection.add_argument("--here", action="store_true")
        scope, _ = selection.parse_known_args(arguments[1:], namespace=DetachedCommandOptions())
        command = BulkCommand("stop" if stop else "restart", account=scope.account, all_rows=scope.all,
            only=tuple(identity for identity in scope.only.split(",") if identity),
            why=scope.why, dry_run=scope.dry_run, here=scope.here)
        report = BulkReport(log, title, command.retry())
        report.targets.update((identity, identity) for identity in command.only)
        report.fixed_targets = frozenset(report.targets)

    def action() -> int:
        try:
            _notify_log_claim(cast(int | None, options.ready_fd), systemd=cast(bool, options.notify_systemd))
        except (OSError, ValueError) as error:
            detail = error.strerror if isinstance(error, OSError) else None
            raise DetachedReadinessFailed(
                f"Could not confirm the detached process is ready. {detail or _error_reason(error)}") from error
        command = cast(AgentsCommand, cast(object, import_module("scripts.agents.shutdown")))
        _ = os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        _ = os.environ.pop("CODEX_THREAD_ID", None)
        return command.main(arguments)

    code = _run_detached_child(action, report, cast(int, options.log_fd), dry_run="--dry-run" in arguments)
    if (received := report.stop_signal) is not None:
        if __name__ == "__main__":
            _ = signal.signal(received, signal.SIG_DFL)
            signal.raise_signal(received)
        return 128 + received
    return code


if __name__ == "__main__":
    raise SystemExit(main())

"""Reboot the shared Herdr session from a terminal outside Herdr."""
from __future__ import annotations

import io
import json
import math
import os
import re
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager, redirect_stderr, redirect_stdout, suppress
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum
from pathlib import Path
from threading import Event, Lock, Thread, get_ident
from typing import TextIO, cast, final, override
from uuid import uuid4

from scripts.agents import restart, shutdown_record, stop_resume
from scripts.herdr import herdr, herdr_types
from scripts.lib import conf
from scripts.lib.atomic_file import write_text_atomic
from scripts.maintenance import herdr_upkeep
from scripts.production import staff


class Step(IntEnum):
    STOP = 1
    SERVER = 2
    MOVE = 3
    START = 4


LABELS = {
    Step.STOP: "Stop the sessions",
    Step.SERVER: "Stop the Herdr server",
    Step.MOVE: "Move session folders",
    Step.START: "Restart the sessions",
}
RETRY = "Nate runs agents reboot again."
ATTACH = "Attach: herdr session attach agents"
FAILURES = (OSError, ValueError, subprocess.SubprocessError, staff.StaffRefused, staff.StaffNotFound,
            herdr_types.HerdrFailure)
MOVE_SUMMARY = re.compile(
    r"session_move: (?:would move|moved) \d+ folders \((\d+\.\d+ GiB)\), kept (\d+) live, refused (\d+) other"
)
MOVE_DETAILS = tuple(f"session_move: {prefix}" for prefix in (
    "copy write failed at ", "saved copy failed at ", "live copy pending at "))


@final
class RunLog(io.TextIOBase):
    """Write each detail line with its time."""

    def __init__(self, file: TextIO) -> None:
        self.file = file
        self.lock = Lock()
        self.pending: dict[int, str] = {}

    def line(self, text: str) -> None:
        _ = self.write(text + "\n")

    @override
    def write(self, s: str, /) -> int:
        with self.lock:
            thread = get_ident()
            lines = (self.pending.pop(thread, "") + s).split("\n")
            if lines[-1]:
                self.pending[thread] = lines[-1]
            self._write_lines(lines[:-1])
        return len(s)

    def finish(self) -> None:
        with self.lock:
            self._write_lines(list(self.pending.values()))
            self.pending.clear()

    def _write_lines(self, lines: list[str]) -> None:
        for line in lines:
            _ = self.file.write(f"{datetime.now().astimezone().isoformat(timespec='milliseconds')} {line}\n")
        if lines:
            self.file.flush()


@final
class StepDisplay:
    def __init__(self, step: Step, terminal: TextIO, log: RunLog) -> None:
        self.step = step
        self.terminal = terminal
        self.log = log
        self.tty = terminal.isatty()
        self.started = time.monotonic()
        self.prefix = f"{step.value} of {len(Step)}  {LABELS[step]}"
        self.lock = Lock()
        self.detail = ""
        self.member_shown = False
        self.spinner = " |"

    def _line(self, spinner: str = "") -> str:
        minutes, seconds = divmod(int(time.monotonic() - self.started), 60)
        clock = f"{minutes}:{seconds:02d}{spinner}"
        if self.step is Step.MOVE and self.detail:
            return f"{self.prefix}  {clock}  {self.detail}"
        detail = f"  {self.detail}" if self.detail else ""
        return f"{self.prefix}{detail}  {clock}"

    def _show(self, *, final: bool = False, spinner: str = "") -> None:
        print(("\r\x1b[K" if self.tty else "") + self._line(spinner if self.tty else ""),
              end="\n" if final or not self.tty else "", file=self.terminal, flush=True)

    def member(self, name: str, count: int, total: int) -> None:
        with self.lock:
            self.member_shown = True
            verb = "stopped" if self.step is Step.STOP else "back"
            self.detail = f"{count} of {total} {verb} - {' '.join(name.split())}"
            self.log.line(self._line())
            self._show(spinner=self.spinner)

    @contextmanager
    def display(self, interval: float) -> Generator[None]:
        self.log.line(self.prefix)
        done = Event()

        def spin() -> None:
            index = 1
            symbols = "|/-\\"
            while not done.wait(interval):
                with self.lock:
                    self.spinner = f" {symbols[index % len(symbols)]}"
                    self._show(spinner=self.spinner)
                index += 1

        thread = Thread(target=spin)
        if self.tty:
            self._show(spinner=self.spinner)
            thread.start()
        try:
            yield
        finally:
            if self.tty:
                done.set()
                thread.join()
                self._show(final=True)
            elif not self.member_shown:
                self._show(final=True)
            self.log.line(self._line())


@dataclass(frozen=True)
class Checkpoint:
    step: Step = Step.STOP
    move_failure: str = ""

    @classmethod
    def read(cls, path: Path) -> Checkpoint:
        try:
            raw = cast(object, json.loads(path.read_text()))
        except FileNotFoundError:
            return cls()
        if isinstance(raw, dict):
            data = cast(dict[str, object], raw)
            step, failure = data.get("step"), data.get("move_failure")
            if type(step) is int and isinstance(failure, str):
                return cls(Step(step), failure)
        raise ValueError("The reboot record is invalid")

    def save(self, path: Path) -> None:
        write_text_atomic(path, json.dumps({"step": self.step, "move_failure": self.move_failure}) + "\n")


@dataclass(frozen=True)
class MoveResult:
    summary: str
    code: int
    details: tuple[str, ...]

    def failure(self) -> str:
        if self.code == 0:
            return "" if self.summary else "Session move did not report its result"
        match = MOVE_SUMMARY.fullmatch(self.summary)
        refused = int(match[3]) if match else 0
        reasons = list(self.details)
        if refused:
            reasons.append(f"{refused} folders could not move" if refused != 1 else "1 folder could not move")
        return ". ".join(reasons) or f"Session move returned status {self.code}"


def move_folders(log: RunLog, *, dry_run: bool = False) -> MoveResult:
    lib = Path.home() / ".claude/scripts/lib"
    command = [str(lib / "py"), str(lib / "session_move.py")]
    if dry_run:
        command.append("--dry-run")
    summary = ""
    details: list[str] = []
    try:
        with subprocess.Popen[str](command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                   errors="replace") as process:
            assert process.stdout is not None
            try:
                while line := cast(str, process.stdout.readline()):
                    line = line.rstrip("\n")
                    log.line(line)
                    if MOVE_SUMMARY.fullmatch(line):
                        summary = line
                    elif line.startswith(MOVE_DETAILS):
                        details.append(line.removeprefix("session_move: "))
                code = process.wait()
            except BaseException:
                with suppress(ProcessLookupError):
                    process.kill()
                raise
    except OSError as error:
        log.line(f"session_move: {error}")
        return MoveResult(summary, 1, (f"Could not run session move. {error}",))
    return MoveResult(summary, code, tuple(details))


def stop_server(log: RunLog) -> None:
    client = herdr.Herdr(herdr.SHARED_SESSION)
    result = herdr.run_program(client.argv("server", "stop"))
    for text in (result.stdout, result.stderr):
        for line in text.splitlines():
            log.line(line)
    if result.returncode:
        status = herdr.run_program(client.argv("status", "server"))
        for text in (status.stdout, status.stderr):
            for line in text.splitlines():
                log.line(line)
        if status.returncode == 0 and "status: stopped" in status.stdout.splitlines():
            return
        herdr.require_success(result, "Could not stop the Herdr server")


def run_bulk(step: Step, path: Path, display: StepDisplay) -> tuple[bool, list[restart.FollowUp]]:
    stopping = step is Step.STOP
    command = "agents stop --all" if stopping else "agents restart"
    report = restart.BulkReport(path, command, "agents reboot", reboot=True, on_finish=display.member)
    try:
        with restart.use_report(report), redirect_stdout(display.log), redirect_stderr(display.log):
            code = restart.stop_all() if stopping else restart.restart()
        pending = set(shutdown_record.read())
        if stopping:
            for key in report.completed - pending:
                report.failure(key, key, "The restart record does not contain this session")
            failed = bool(code or report.follow_ups())
        else:
            failed = bool(code or pending or any(issue.operator for issue in report.issues.values()))
    except FAILURES as error:
        report.failure("", command, str(error))
        failed = True
    if failed and not report.follow_ups():
        report.failure("", command, "Could not confirm that the step finished")
    if not report.completed:
        verb = "stopped" if stopping else "back"
        display.detail = f"0 of {len(report.targets)} {verb}"
    display.log.finish()
    return failed, list(report.follow_ups().values())


def show_plan(terminal: TextIO, log: RunLog) -> bool:
    planned = move_folders(log, dry_run=True)
    match = MOVE_SUMMARY.fullmatch(planned.summary)
    if planned.code == 0 and match is not None:
        line = f"Plan: {match[1]} in idle folders now, {match[2]} live folders sized after the stop."
        complete = True
    else:
        line = "Plan: Incomplete. See the session_move details in the log."
        complete = False
    print(line, file=terminal, flush=True)
    log.line(line)
    return complete


def report_pending(path: Path, log: RunLog, issues: list[restart.FollowUp]) -> None:
    try:
        pending = shutdown_record.read()
        if not pending:
            return
        record = staff.initial_snapshot()
    except FAILURES as error:
        log.line(f"Could not read the saved sessions. {error}")
        return
    report = restart.BulkReport(path, "agents reboot", "agents reboot")
    named = {issue.name for issue in issues}
    for key in pending:
        row = record.rows_by_id.get(staff.StaffId(key))
        if row is not None:
            report.track(row, record)
        name = report.targets.get(key, key)
        if name not in named:
            issues.append(restart.FollowUp(name, "The reboot stopped before it could restart this session",
                                           RETRY, operator=True))


def check_sessions(path: Path, log: RunLog) -> tuple[list[staff.StaffRow], restart.BulkReport]:
    report = restart.BulkReport(path, "agents stop --all", "agents reboot", reboot=True)
    try:
        with restart.use_report(report), redirect_stdout(log), redirect_stderr(log):
            record = staff.initial_snapshot()
            reading = restart.live_rows(record, herdr.Herdr(herdr.SHARED_SESSION))
        if isinstance(reading, restart.PartialLiveRows) and not report.issues:
            report.failure("", "Herdr", "Could not check all sessions")
        for row in reading.rows:
            report.track(row, record)
        return reading.rows, report
    except FAILURES as error:
        report.failure("", "Herdr", f"Could not check the sessions. {error}")
        return [], report


def run_steps(checkpoint: Path, path: Path, terminal: TextIO, log: RunLog,
              interval: float) -> tuple[bool, list[restart.FollowUp]]:
    state = Checkpoint.read(checkpoint)
    issues: list[restart.FollowUp] = []
    failed = False
    try:
        with herdr_upkeep.upkeep_lock(blocking=True) as hold:
            if hold is None:
                raise OSError("Could not hold the Herdr upkeep job")
            checked: tuple[list[staff.StaffRow], restart.BulkReport] | None = None
            if state.step is Step.SERVER:
                rows, report = check_sessions(path, log)
                if rows and not report.issues:
                    state = Checkpoint(Step.STOP, state.move_failure)
                    state.save(checkpoint)
                else:
                    checked = rows, report
            if state.step is Step.STOP:
                _ = show_plan(terminal, log)
            for step in Step:
                if step < state.step:
                    continue
                display = StepDisplay(step, terminal, log)
                with display.display(interval):
                    if step in (Step.STOP, Step.START):
                        failed, issues = run_bulk(step, path, display)
                    elif step is Step.SERVER:
                        rows, report = checked if checked is not None else check_sessions(path, log)
                        for row in rows:
                            report.failure(row.id, row.name, "The session still runs")
                        if not report.issues:
                            try:
                                stop_server(log)
                            except FAILURES as error:
                                report.failure("", "Herdr", str(error))
                        issues = list(report.issues.values())
                        failed = bool(issues)
                        display.detail = "Could not stop the server" if failed else "Stopped"
                    else:
                        moved = move_folders(log)
                        state = Checkpoint(step, moved.failure())
                        display.detail = moved.summary or "Could not complete session move"
                if failed:
                    if step is not Step.START:
                        report_pending(path, log, issues)
                    break
                if step is Step.START:
                    checkpoint.unlink()
                else:
                    state = Checkpoint(Step(step + 1), state.move_failure)
                    state.save(checkpoint)
    except KeyboardInterrupt:
        failed = True
        issues.append(restart.FollowUp("agents reboot", "The reboot was interrupted", RETRY, operator=True))
        report_pending(path, log, issues)
    if state.move_failure:
        issues.append(restart.FollowUp("session_move", state.move_failure,
                                       "The verify owner checks the log."))
    return failed or bool(state.move_failure), issues


def run(*, dry_run: bool = False) -> int:
    if os.environ.get("HERDR_PANE_ID") or os.environ.get("HERDR_ENV") == "1":
        print("Run agents reboot in a terminal outside Herdr.")
        return 2
    terminal = sys.stdout
    settings = conf.read_or_repository("shutdown")
    folder = settings.value("REBOOT_LOG_FOLDER", str, default="reboot")
    if folder in ("", ".", "..") or Path(folder).name != folder:
        raise ValueError("REBOOT_LOG_FOLDER must be one folder name")
    state = stop_resume.reboot_state_directory()
    directory = state.with_name(folder)
    interval = settings.number("REBOOT_SPINNER_SECONDS")
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("REBOOT_SPINNER_SECONDS must be finite and greater than zero")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{datetime.now():%Y%m%dT%H%M%S}-{uuid4().hex}.log"
    with path.open("x", encoding="utf-8") as stream:
        log = RunLog(stream)
        print(path, file=terminal, flush=True)
        log.line(str(path))
        issues: list[restart.FollowUp] = []
        try:
            latest = path.with_suffix(".link")
            latest.symlink_to(path.name)
            try:
                _ = latest.replace(directory / "latest.log")
            finally:
                latest.unlink(missing_ok=True)
            if dry_run:
                return int(not show_plan(terminal, log))
            with stop_resume.reboot_lock(exclusive=True):
                if herdr.shared_session() != herdr.SHARED_SESSION:
                    raise ValueError("The shared Herdr session must be named agents")
                checkpoint = state / "reboot.json"
                if not checkpoint.exists():
                    # Move any checkpoint from the earlier log folder layout.
                    try:
                        _ = (directory / checkpoint.name).replace(checkpoint)
                    except FileNotFoundError:
                        Checkpoint().save(checkpoint)
                failed, issues = run_steps(checkpoint, path, terminal, log, interval)
        except stop_resume.RebootLockBusy:
            failed = True
            issues.append(restart.FollowUp("agents reboot", "A reboot or session start holds the lock", RETRY, operator=True))
        except (KeyboardInterrupt, *FAILURES) as error:
            failed = True
            reason = "The reboot was interrupted" if isinstance(error, KeyboardInterrupt) else str(error)
            issues.append(restart.FollowUp("agents reboot", reason, RETRY, operator=True))
        log.finish()
        lines = [issue.line() for issue in sorted(issues, key=lambda issue: (issue.name, issue.reason))]
        lines.extend(["Nate has a step." if any(issue.operator for issue in issues) else "Nate has no step.", ATTACH])
        for line in lines:
            print(line, file=terminal, flush=True)
            log.line(line)
        return int(failed)

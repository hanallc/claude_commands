#!/usr/bin/env python3
"""Stop every Codex worker on this machine when Codex capacity ends.

quota_alert.py starts `stop` when no-worker mode goes on. Each Codex worker
stops with proof, each open pass of a stopped worker ends as canceled, and each
affected unit director gets one report. Claude workers are not touched.

No command starts workers again. When capacity returns, quota_alert.py sends
the notice, and the next assign of a unit director starts its worker.

A live detached run of a stopped worker continues. Its result goes to the
director, because delivery to a stopped or closed worker goes to the holder.
That run keeps the stopped worker's pane open; the run end closes the pane.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from threading import Event, Lock
from typing import Literal

from scripts.agents import detached_runs, restart
from scripts.direct import progress_history, recorder_state
from scripts.herdr import herdr, herdr_types, turn_end, worker_panes
from scripts.lib import conf
from scripts.production import broadcast, session_staff, showrunners, staff, worker
from scripts.whoami import quota_state

SENDER = "worker-capacity"
# A dispatch holds the worker lock while it submits; the stop waits this long for it.
LOCK_WAIT_SECONDS = 60
USAGE = "usage: worker_capacity.py stop"
_PANE_RECOVERY = Lock()


@dataclass(frozen=True)
class PassesNotCanceled:
    reason: str


@dataclass(frozen=True)
class PaneOpen:
    """The stopped worker's pane did not close. A pane kept by a live detached run closes when the run ends."""

    close: Literal["kept", "uncertain"]
    reason: str


@dataclass(frozen=True)
class WorkerStopped:
    """The worker process is proved absent."""

    name: str
    process: Literal["stopped", "absent"]
    canceled_passes: int | PassesNotCanceled
    detached_runs: tuple[str, ...] = ()
    pane: PaneOpen | None = None


@dataclass(frozen=True)
class WorkerNotStopped:
    name: str
    reason: str


class StopSweep:
    def __init__(self) -> None:
        self.mode_ended: Event = Event()

    def active(self) -> bool:
        if not quota_state.no_worker_mode():
            self.mode_ended.set()
        return not self.mode_ended.is_set()


class StopSweepAtUse(Enum):
    UNCONDITIONAL = "stop outside a quota sweep"


class StopSkipped(Enum):
    MODE_ENDED = "no-worker mode ended"
    NO_OPEN_WORK = "the worker is closed and has no open pass"


def _observe(parent: staff.StaffRow, row: staff.StaffRow) -> worker_panes.WorkerPane:
    states = worker_panes.observe(parent.id, staff.local_snapshot())
    return next((state for state in states if state.row.id == row.id),
                worker_panes.UncertainWorker(row, "worker is absent from staff"))


def _stop_seconds() -> int:
    settings = conf.read("shutdown")
    return sum(settings.whole(key) for key in ("stop_grace_seconds", "kill_grace_seconds", "move_stop_margin_seconds"))


def stop_worker(parent: staff.StaffRow, row: staff.StaffRow, client: herdr.Herdr, *,
                sweep: StopSweep | StopSweepAtUse = StopSweepAtUse.UNCONDITIONAL,
                ) -> WorkerStopped | WorkerNotStopped | Literal[StopSkipped.MODE_ENDED]:
    """Stop one worker with proof, then end its open passes as canceled."""
    name = staff.WorkerName.parse(row.name)
    with worker_panes.worker_lock(parent.id, name, deadline=time.monotonic() + LOCK_WAIT_SECONDS) as hold:
        if isinstance(sweep, StopSweep) and not sweep.active():
            return StopSkipped.MODE_ENDED
        if isinstance(hold, worker_panes.Kept):
            return WorkerNotStopped(row.name, hold.blocker)
        state = _observe(parent, row)
        if isinstance(state, (worker_panes.ProcessUnknownWorker, worker_panes.UncertainWorker)):
            return WorkerNotStopped(row.name, state.reason)
        try:
            run = worker_panes.worker_run(row)
            with suppress(FileNotFoundError):
                _ = recorder_state.read_state(run / recorder_state.STATE_FILENAME)
        except recorder_state.OldRecorderState as error:
            return WorkerNotStopped(row.name, str(error))
        process: Literal["stopped", "absent"] = "absent"
        try:
            stop_attempts = conf.read("shutdown").whole("worker.stop_attempts") if isinstance(state, worker_panes.OpenWorker) else 0
        except conf.Unusable as error:
            return WorkerNotStopped(row.name, str(error))
        for _ in range(stop_attempts):
            if not isinstance(state, worker_panes.OpenWorker):
                break
            if isinstance(sweep, StopSweep) and not sweep.active():
                return StopSkipped.MODE_ENDED
            process = "stopped"
            _ = restart.stop_row(state.row, client, deadline=time.monotonic() + _stop_seconds())
            state = _observe(parent, row)
        if isinstance(state, (worker_panes.ProcessUnknownWorker, worker_panes.UncertainWorker)):
            return WorkerNotStopped(row.name, state.reason)
        if isinstance(state, worker_panes.OpenWorker):
            return WorkerNotStopped(row.name, "the stop did not prove that the worker stopped")
        canceled: int | PassesNotCanceled
        try:
            canceled = progress_history.cancel_worker_passes(run, row.id)
        except (OSError, ValueError, SystemExit, recorder_state.OldRecorderState) as error:
            canceled = PassesNotCanceled(" ".join(str(error).split()))
        runs = run / detached_runs.DETACHED_RUNS_DIR
        detached = tuple(f"{record['run_id']}: result {runs / (record['run_id'] + '.json')}; log {record['log']}"
                         for record in detached_runs.live_runs_for_staff(runs, frozenset({row.id})))
        pane: PaneOpen | None = None
        try:
            # Recovery can redirect standard error. Only one recovery can do that at a time.
            with _PANE_RECOVERY:
                recovery = turn_end.recover_stopped_in_run(run, row.id, hold=hold)
        except (herdr_types.HerdrFailure, OSError, ValueError, staff.StaffRefused, recorder_state.OldRecorderState) as error:
            return WorkerStopped(row.name, process, canceled, detached, PaneOpen("uncertain", str(error)))
        if recovery in (worker.CompletionRecovery.PENDING_CLOSE, worker.CompletionRecovery.WAITING_ON_REPLY,
                        worker.CompletionRecovery.WAITING_ON_RUNS):
            return WorkerStopped(row.name, process, canceled, detached, PaneOpen("kept", recovery.value))
        if recovery is worker.CompletionRecovery.NO_PENDING_NOTICE and isinstance(state, worker_panes.StoppedWorker):
            # The stop is complete here. The pane closes only when the close checks permit it.
            try:
                closed = worker_panes.close(parent.id, name, lock=hold)
            except (herdr_types.HerdrFailure, OSError, ValueError, recorder_state.OldRecorderState) as error:
                closed = worker_panes.UncertainWorker(row, " ".join(str(error).split()))
            if isinstance(closed, worker_panes.Kept):
                pane = PaneOpen("kept", closed.blocker)
            elif isinstance(closed, worker_panes.UncertainWorker):
                pane = PaneOpen("uncertain", closed.reason)
        if not isinstance(canceled, PassesNotCanceled) and pane is None:
            worker_panes.forget_restart(row)
        return WorkerStopped(row.name, process, canceled, detached, pane)


def report_text(outcomes: list[WorkerStopped | WorkerNotStopped]) -> str:
    """Write one report for a unit director; the empty string when nothing changed."""
    stopped = [item for item in outcomes if isinstance(item, WorkerStopped)]
    lines: list[str] = []
    if names := [item.name for item in stopped if item.process == "stopped"]:
        lines.append(f"Stopped: {', '.join(names)}.")
    if names := [item.name for item in stopped if isinstance(item.canceled_passes, int) and item.canceled_passes]:
        lines.append(f"Passes ended as canceled: {', '.join(names)}. Continue that work yourself from the worktree.")
    for item in stopped:
        if isinstance(item.canceled_passes, PassesNotCanceled):
            lines.append(f"Pass stays open: {item.name}: {item.canceled_passes.reason}.")
        lines.extend(f"Detached run of {item.name} continues: {run}." for run in item.detached_runs)
        if item.pane is not None:
            lines.append(_pane_line(item, item.pane) + ".")
    lines.extend(f"Not proved stopped: {item.name}: {item.reason}. Send it no work."
                 for item in outcomes if isinstance(item, WorkerNotStopped))
    if not lines:
        return ""
    return "\n".join(["Codex workers have no capacity. This is the stop report for your Codex workers.", *lines])


def _pane_line(item: WorkerStopped, pane: PaneOpen) -> str:
    if pane.close == "uncertain":
        return f"Pane close is uncertain: {item.name}: {pane.reason}"
    end = "; it closes when its detached run ends" if item.detached_runs else ""
    return f"Pane stays open: {item.name}: {pane.reason}{end}"


def _fault_lines(parent: staff.StaffRow, outcomes: list[WorkerStopped | WorkerNotStopped]) -> list[str]:
    stopped = [item for item in outcomes if isinstance(item, WorkerStopped)]
    return ([f"{parent.name}: {item.name}: not proved stopped: {item.reason}"
             for item in outcomes if isinstance(item, WorkerNotStopped)]
            + [f"{parent.name}: {item.name}: pass not canceled: {item.canceled_passes.reason}"
               for item in stopped if isinstance(item.canceled_passes, PassesNotCanceled)]
            + [f"{parent.name}: {_pane_line(item, item.pane)}" for item in stopped if item.pane is not None])


def _send(recipient: str, text: str) -> bool:
    failure = broadcast.send(recipient, SENDER, text)
    if failure:
        with _PANE_RECOVERY:
            print(f"worker-capacity: report to {recipient} failed: {failure}", file=sys.stderr, flush=True)
    return not failure


def _stop_observed(parent: staff.StaffRow, state: worker_panes.WorkerPane, record: staff.StaffSnapshot,
                   scanned: dict[str, list[str]], client: herdr.Herdr, sweep: StopSweep,
                   ) -> WorkerStopped | WorkerNotStopped | StopSkipped:
    try:
        if isinstance(state, worker_panes.ClosedWorker) and isinstance(progress_history.active_dispatch(
                state.row.id, worker_panes.worker_run(state.row, record=record, scanned=scanned)),
                progress_history.NoActiveDispatch):
            return StopSkipped.NO_OPEN_WORK
        if not sweep.active():
            return StopSkipped.MODE_ENDED
        return stop_worker(parent, state.row, client, sweep=sweep)
    except (herdr_types.HerdrFailure, staff.StaffRefused, OSError, ValueError, recorder_state.OldRecorderState) as error:
        return WorkerNotStopped(state.row.name, " ".join(str(error).split()))


def stop() -> int:
    """Stop the Codex workers of every local parent and report to each affected director."""
    sweep = StopSweep()
    if not sweep.active():
        print("No-worker mode ended during the stop.", flush=True)
        return 0
    record = staff.snapshot()
    parents = session_staff.pane_parents(record)
    observed = worker_panes.observe_parents(parents, record)
    scanned = worker_panes.parent_run_index(record)
    client = herdr.Herdr(herdr.shared_session())
    targets = [(parent, state) for parent in parents for state in observed[parent.id]
               if isinstance(record.backends.get(state.row.id), staff.CodexBackend)]
    if not targets:
        return 0

    faults: list[str] = []
    delivered = True
    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        pending: dict[Future[WorkerStopped | WorkerNotStopped | StopSkipped], staff.StaffRow] = {}
        by_parent: dict[staff.StaffId, list[Future[WorkerStopped | WorkerNotStopped | StopSkipped]]] = {}
        for parent, state in targets:
            future = pool.submit(_stop_observed, parent, state, record, scanned, client, sweep)
            pending[future] = parent
            by_parent.setdefault(parent.id, []).append(future)
        for future in as_completed(pending):
            parent = pending[future]
            siblings = by_parent.get(parent.id, [])
            if not siblings or not all(item.done() for item in siblings):
                continue
            del by_parent[parent.id]
            outcomes = [result for item in siblings
                        if isinstance(result := item.result(), (WorkerStopped, WorkerNotStopped))]
            faults.extend(_fault_lines(parent, outcomes))
            if (text := report_text(outcomes)) and sweep.active():
                for line in [f"{parent.name}:", *text.splitlines()]:
                    print(line, flush=True)
                if sweep.active():
                    delivered = _send(str(parent.id), text) and delivered
    if faults and sweep.active():
        text = "\n".join(["Codex worker stop is incomplete. A Codex worker that still runs can spend credits.", *faults])
        for line in text.splitlines():
            print(line, file=sys.stderr, flush=True)
        recipient = showrunners.load_settings()["faults_to"]
        if sweep.active():
            delivered = _send(recipient, text) and delivered
    if not sweep.active():
        print("No-worker mode ended during the stop.", flush=True)
    return 0 if delivered and not faults else 1


def main(arguments: list[str]) -> int:
    if arguments != ["stop"]:
        print(USAGE, file=sys.stderr, flush=True)
        return 2
    try:
        return stop()
    except (staff.StaffUnreadable, herdr_types.HerdrFailure, OSError, ValueError) as error:
        print(f"worker-capacity: {' '.join(str(error).split())}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

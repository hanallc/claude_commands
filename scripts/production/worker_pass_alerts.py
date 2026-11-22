"""Build keyed alerts for passes that need the director's attention."""

import hashlib
import json
import shlex
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from importlib import import_module
from pathlib import Path
from typing import Protocol, cast

from scripts.direct import pass_record, recorder_state
from scripts.lib.atomic_file import create_text_atomic
from scripts.production import staff, worker_admission
from scripts.production.staff import StaffId


ACCEPTED_OUTCOMES = frozenset(("sent", "skipped", "queued", "kept", "replaced"))


class AlertReason(StrEnum):
    NO_DONE = "no DONE"
    STALE_DONE = "DONE for another pass"
    RUN_REPLY = "waiting on run reply"
    BLOCKED = "worker pane is blocked"


class WaitingForRunsReceipt(Protocol):
    @property
    def results_pending(self) -> bool: ...


class WorkerReceipts(Protocol):
    DispatchComplete: type[object]
    DispatchWaitingForRuns: type[WaitingForRunsReceipt]

    def dispatch_receipt(self, row: staff.StaffRow, line: str, dispatch_id: str, *,
                         run: Path, require_transcript: bool, require_done: bool) -> object: ...


def recovery_instruction(director: StaffId, worker_name: str, run: Path, instance: str, *,
                         record: staff.StaffSnapshot | None = None) -> str:
    """Read the receipt and give the director a recovery instruction."""
    from scripts.herdr import herdr, herdr_types, worker_panes

    pane: worker_panes.WorkerPane | worker_panes.WorkerNotObserved = worker_panes.WorkerNotObserved.CURRENT
    try:
        if record is None:
            record = staff.snapshot()
        row = worker_admission.find_worker(record, director, worker_name)
        if row is not None:
            passed = worker_panes.worker_pass(row, run, record=record)
            delivery = pass_record.dispatch_delivery(passed)
            if (passed.get("instance_id") == instance
                    and delivery.line and delivery.completion_id and delivery.reply_file):
                worker = cast(WorkerReceipts, cast(object, import_module("scripts.production.worker")))
                receipt = worker.dispatch_receipt(row, delivery.line, delivery.completion_id,
                                                  run=run, require_transcript=True, require_done=True)
                if isinstance(receipt, worker.DispatchComplete):
                    return "The result is complete. The completion notice follows."
                if isinstance(receipt, worker.DispatchWaitingForRuns) and receipt.results_pending:
                    return "The pass is waiting for detached results. The completion notice follows."
            pane = worker_panes.observe_worker(director, row, record)
    except (OSError, ValueError, herdr_types.HerdrFailure, recorder_state.OldRecorderState):
        pass

    command = "~/.claude/scripts/lib/py ~/.claude/scripts/production/worker.py"
    if not isinstance(pane, worker_panes.OpenWorker) or pane.status is herdr.AgentStatus.UNKNOWN:
        return f"The pane state for {worker_name} is unknown.\nRun {command} list all"
    if pane.status is herdr.AgentStatus.BLOCKED:
        arguments = shlex.join(["answer", worker_name, "--question", "--text", "<answer>"])
        return f"Replace <answer> with your answer to the worker's question.\nRun {command} {arguments}"
    arguments = shlex.join(["answer", worker_name, "--text", "Continue this pass and send the required DONE."])
    return f"Run {command} {arguments}"


def alert_key(instance: str, reason: AlertReason) -> str:
    return f"worker-pass-alert:{instance}:{reason.name.lower()}"


@dataclass(frozen=True)
class PassAlert:
    director: StaffId
    worker: StaffId
    run: Path
    instance: str
    reason: AlertReason
    details: str = ""


def alert_arguments(alert: PassAlert, *, saved: Path | None = None) -> list[str] | None:
    """Use the current pass and pane to build the recovery instruction."""
    from scripts.herdr import worker_panes

    try:
        record = staff.snapshot()
        row = record.by_id(alert.worker)
        if row is None:
            return None
        if row.parent != staff.ReportsTo(alert.director):
            if saved is not None:
                saved.unlink(missing_ok=True)
            return None
        passed = worker_panes.worker_pass(row, alert.run, record=record)
        if not passed:
            return None
        if passed.get("instance_id") != alert.instance or passed.get("status") != "active":
            if saved is not None:
                saved.unlink(missing_ok=True)
            return None
    except (OSError, ValueError, recorder_state.OldRecorderState) as error:
        print(f"worker-pass-alert: {saved or alert.worker}: {error}", file=sys.stderr)
        return None

    route = f"staff:{alert.director}"
    message = f"{row.name}: pass {alert.instance}: {alert.reason}."
    if alert.details:
        message += f"\n{alert.details}"
    message += "\n" + recovery_instruction(alert.director, row.name, alert.run, alert.instance, record=record)
    return ["--to", route, "--from", "worker-pass-alert", "--text", message,
            "--key", alert_key(alert.instance, alert.reason),
            "--recipient-key", route, "--repeat-minutes", "52596000"]


def _pending_directory() -> Path:
    return staff.record_path().parent / "worker-pass-alerts"


def save_pending(row: staff.StaffRow, director: StaffId, run: Path, instance: str,
                 reason: AlertReason, details: str) -> None:
    """Save the alert before the first send attempt."""
    key = hashlib.sha256(f"{instance}:{reason.name}".encode()).hexdigest()
    path = _pending_directory() / str(row.id) / f"{key}.json"
    if path.exists():
        return
    _ = create_text_atomic(path, json.dumps({
        "director": director, "run": str(run),
        "instance": instance, "reason": reason.value, "details": details,
    }) + "\n")


def pending_paths(*, worker: StaffId | None = None) -> Iterator[Path]:
    directory = _pending_directory()
    return directory.glob("*/*.json") if worker is None else (directory / str(worker)).glob("*.json")


def pending_arguments(path: Path) -> list[str] | None:
    """Read the saved alert and use the current pane to build its command."""
    try:
        fields = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
        match fields:
            case {"director": str(director), "run": str(run),
                  "instance": str(instance), "reason": str(reason), "details": str(details)}:
                alert = PassAlert(StaffId(director), StaffId(path.parent.name), Path(run),
                                  instance, AlertReason(reason), details)
            case _:
                raise ValueError("invalid pass alert")
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        print(f"worker-pass-alert: {path}: {error}", file=sys.stderr)
        return None
    return alert_arguments(alert, saved=path)

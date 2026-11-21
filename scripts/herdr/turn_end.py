#!/usr/bin/env python3
"""Save a worker turn and notify its director through one completion rule."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import hashlib
import io
import json
import re
import subprocess
import sys
from collections.abc import Generator, Iterable
from contextlib import AbstractContextManager, ExitStack, redirect_stderr
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from enum import Enum, StrEnum
from pathlib import Path
from typing import Callable, cast

from scripts.agents import detached_runs, shutdown_record, worker_lifecycle
from scripts.direct import progress_history, recorder_state
from scripts.herdr import herdr_types
from scripts.lib import scratch, unit_runs
from scripts.herdr import herdr, worker_panes
from scripts.production import staff, worker, worker_admission

COMPLETION_NOTICE_REPEAT_MINUTES = 52_596_000


class EventKind(StrEnum):
    TURN_END = "turn end"
    RUN_FINISHED = "run finished"
    STAGE_READY = "stage ready"


@dataclass(frozen=True)
class WorkerEvent:
    thread_id: str
    kind: EventKind = EventKind.TURN_END
    reply: str = ""


class TurnOutcome(StrEnum):
    DONE = "done"
    STOPPED = "stopped"
    LATE_DONE = "late DONE"
    NO_DONE = "no DONE"
    BUSY = "busy"
    UNCERTAIN = "uncertain"
    ERROR = "error"
    WAITING_ON_RUNS = "waiting on runs"
    WAITING_ON_FRIEND = "waiting on friend"
    WAITING_ON_REPLY = "waiting on run reply"
    WAITING_ON_STAGE_TWO = "waiting on final diff"
    RETRY_EVIDENCE = "completion evidence needs another read"
    SUBMITTED = "submitted"
    IGNORED = "ignored"


def hook_event(value: dict[str, object]) -> WorkerEvent:
    """Convert each native hook input at the boundary."""
    if value.get("hook_event_name") == "Stop":
        return WorkerEvent(str(value.get("session_id", "")), reply=str(value.get("last_assistant_message") or ""))
    kind = value.get("event")
    if kind in (EventKind.RUN_FINISHED, EventKind.STAGE_READY):
        return WorkerEvent(str(value.get("thread_id", "")), EventKind(str(kind)))
    return WorkerEvent("")


def _friend_pending(client: herdr.Herdr, state: worker_panes.WorkerPane | worker_panes.WorkerNotObserved,
                    row: staff.StaffRow) -> bool:
    if not isinstance(state, worker_panes.OpenWorker):
        return False
    panes = client.panes()
    tab = next((pane.get("tab_id") for pane in panes if pane["pane_id"] == state.pane), "")
    kind = "codex" if isinstance(staff.backend(row), staff.CodexBackend) else "claude"
    address = ("codex:" if kind == "codex" else "session:") + str(row.current_session)
    for pane in panes:
        if not (pane.get("tab_id") == tab and pane["pane_id"] != state.pane
                and client.readiness(pane["pane_id"])["agent_status"] == herdr.AgentStatus.WORKING):
            continue
        try:
            native = client.native_pane_process(pane["pane_id"], kind, herdr.PROGRAM_TIMEOUT_SECONDS, pane_row=pane)
        except herdr_types.HerdrFailure:
            continue
        backend = (staff.CodexBackend(Path(native.environment["CODEX_HOME"])) if kind == "codex"
                   else staff.ClaudeBackend())
        source = worker_admission.transcript_source(replace(row, current_session=staff.native_id(native.current_identity)), backend)
        prompts = [entry for entry in worker_admission.transcript_entries(source, set())
                   if isinstance(entry, str) and entry.startswith("user:\n")]
        if prompts and f"--to {address} --text " in prompts[-1]:
            return True
    return False


def _kept_pane_anomaly(row: staff.StaffRow, state: worker_panes.WorkerPane | worker_panes.WorkerNotObserved,
                       reason: str) -> str:
    """Keep the close reason and pane in the director's one result message."""
    pane = state.pane if isinstance(state, (worker_panes.OpenWorker, worker_panes.StoppedWorker,
                                           worker_panes.ProcessUnknownWorker)) else "unobserved"
    reason = reason.removeprefix(f"anomaly: {row.name}: ")
    message = f"anomaly: {row.name}: {' '.join(reason.splitlines())}; kept pane {pane}"
    print(message, file=sys.stderr)
    return message


@dataclass(frozen=True)
class TurnDecision:
    outcome: TurnOutcome
    send_notice: Callable[[], TurnOutcome | None]


def _no_notice() -> None:
    pass


def _same_completion(saved: progress_history.HookCompletion | progress_history.NoHookCompletion,
                     expected: progress_history.HookCompletion | progress_history.NoHookCompletion) -> bool:
    return (isinstance(saved, progress_history.HookCompletion)
            and isinstance(expected, progress_history.HookCompletion)
            and (saved.outcome, saved.reply_file) == (expected.outcome, expected.reply_file))


def _notice_stamp(outcome: TurnOutcome, reply: str, runs: tuple[str, ...] = ()) -> str:
    return hashlib.sha256("\n".join((outcome.value, reply, *runs)).encode()).hexdigest()


def _completion_notice_stamp(outcome: TurnOutcome) -> str:
    return f"completion:{outcome.value}"


def _prepare_notice(worker_name: str, parent: staff.StaffRow, run: Path, passed: dict[str, object],
                    instance: str, outcome: TurnOutcome, path: Path, reply: str, details: str,
                    runs: tuple[str, ...] = (), *, notice_type: str = "", repeat: bool = False,
                    saved_reply_path: bool = False) -> TurnDecision:
    """Prepare a notice for delivery outside the worker lock."""
    from scripts.message import send

    stamp = (_completion_notice_stamp(outcome) if notice_type or repeat
             else _notice_stamp(outcome, reply, runs))
    if not repeat and (passed.get("hook_notice") == stamp or notice_type and completion_notice_delivered(passed)):
        return TurnDecision(outcome, _no_notice)
    phase = passed.get("phase", {})
    phase_id = str(cast(dict[str, object], phase).get("id", "")) if isinstance(phase, dict) else ""
    address = ("codex:" if isinstance(staff.backend(parent), staff.CodexBackend) else "session:") + str(parent.current_session)
    message = f"{worker_name}: phase {phase_id}: {outcome.value}"
    if reply or saved_reply_path:
        message += f"; reply {path}"
    message += f"\nPass {instance}." if outcome == TurnOutcome.STOPPED else f"; pass {instance}"
    if runs:
        message += "; runs " + ", ".join(runs)
    if details:
        message += "\n" + details
    completion = progress_history.hook_completion(passed)
    if isinstance(completion, progress_history.HookCompletion) and completion.sender_native_id:
        sender = send.read_sender_stamp(completion.sender_native_id)
    else:
        member = staff.snapshot().by_id(staff.StaffId(str(passed.get("worker_id", ""))))
        identity = str(member.current_session) if member is not None else str(passed.get("worker_id", ""))
        sender = (send.ClaudeSender(identity) if member is not None and isinstance(staff.backend(member), staff.ClaudeBackend)
                  else send.CodexSender(identity))
    key = f"worker-completion:{instance}:{notice_type}"
    deduplicate = ["--key", key, "--recipient-key", f"staff:{parent.id}",
                   "--repeat-minutes", str(COMPLETION_NOTICE_REPEAT_MINUTES)] if notice_type else []
    options = send.parse(["--to", address, "--from", worker_name, "--text", message, *deduplicate])
    options = options._replace(sender_origin=send.ForwardedSender(sender), shown_sender=send.ShownSender(worker_name))

    def deliver() -> None:
        current = staff.snapshot().by_id(parent.id)
        if current is None:
            raise ValueError(f"{parent.id}: notice recipient is not in staff")
        address = ("codex:" if isinstance(staff.backend(current), staff.CodexBackend) else "session:") + str(current.current_session)
        result = send.send(options._replace(selection=send.RosterLookup(address)))
        if result.outcome not in ("sent", "skipped"):
            raise ValueError(result.detail)
        if outcome == TurnOutcome.STOPPED:
            progress_history.record_dispatch_notice(run, instance, stamp, stopped_pass=True)
        else:
            progress_history.record_dispatch_notice(run, instance, stamp)

    def send_notice() -> None:
        digest = hashlib.sha256(instance.encode()).hexdigest()
        with worker.CompletionArtifacts(parent, run).lock(run / f".{digest}.notice.lock"):
            if outcome == TurnOutcome.STOPPED:
                if not progress_history.stopped_notice_delivered(run, instance):
                    deliver()
                return
            record = staff.snapshot()
            member = record.by_id(staff.StaffId(str(passed.get("worker_id", ""))))
            if member is None:
                if not notice_type:
                    deliver()
                return
            current = worker_panes.worker_pass(member, run, record=record)
            saved = progress_history.hook_completion(current)
            if isinstance(saved, progress_history.HookCompletion) and saved.outcome == TurnOutcome.STOPPED:
                return
            if notice_type and (current.get("instance_id") != instance
                                or not _same_completion(saved, completion)):
                return
            if not repeat and current.get("instance_id") == instance and (
                    current.get("hook_notice") == stamp or completion_notice_delivered(current)):
                return
            deliver()

    return TurnDecision(outcome, send_notice)


def _completion_reply(completion: progress_history.HookCompletion) -> str:
    if completion.outcome == TurnOutcome.STOPPED:
        return ""
    try:
        return Path(completion.reply_file).read_text()
    except FileNotFoundError:
        if completion.outcome != TurnOutcome.ERROR:
            raise
        return "\n".join(completion.details)


def completion_notice_delivered(passed: dict[str, object]) -> bool:
    completion = progress_history.hook_completion(passed)
    if not isinstance(completion, progress_history.HookCompletion):
        return False
    if completion.finished or passed.get("hook_notice") == _completion_notice_stamp(TurnOutcome(completion.outcome)):
        return True
    if not passed.get("hook_notice"):
        return False
    try:
        reply = _completion_reply(completion)
    except FileNotFoundError:
        return False
    return passed.get("hook_notice") == _notice_stamp(TurnOutcome(completion.outcome), reply)


def _stopped_notice(row: staff.StaffRow, parent: staff.StaffRow, run: Path, passed: dict[str, object],
                    completion: progress_history.HookCompletion) -> TurnDecision:
    return _prepare_notice(row.name, parent, run, {**passed, "hook_completion": asdict(completion)},
                           str(passed["instance_id"]), TurnOutcome.STOPPED, Path(completion.reply_file),
                           "", "\n".join(completion.details), notice_type=TurnOutcome.STOPPED,
                           saved_reply_path=bool(completion.reply_file)
                           and completion.stopped_reply is progress_history.StoppedReplyDisposition.SAVED)


def _detached_stop_completion(decision: TurnDecision) -> worker_panes.StoppedRunPassCompletion:
    def complete() -> worker_panes.StoppedPassCleanup:
        if decision.send_notice() == TurnOutcome.RETRY_EVIDENCE:
            return worker_panes.StoppedPassCleanup.RETRY
        return worker_panes.StoppedPassCleanup.COMPLETE

    return worker_panes.StoppedRunPassCompletion(decision.outcome, complete)


def _recorded_stop_notice(run: Path, notice: progress_history.StoppedPassNotice) -> worker_panes.StoppedRunPassCompletion:
    passed = progress_history.pass_for_finalization(run, notice.instance_id)
    delivery = progress_history.dispatch_delivery(passed)
    if notice.delivered and not delivery.actions.finish_run:
        return _detached_stop_completion(TurnDecision(TurnOutcome.STOPPED, _no_notice))
    record = staff.snapshot()
    director = record.by_id(notice.director_id)
    if director is None:
        raise ValueError(f"{notice.director_id}: stopped pass notice recipient is not in staff")
    recipient_fields: dict[str, object] = {"worker_id": notice.worker_id, "phase": {"id": notice.phase_id}}
    decision = _prepare_notice(notice.worker_name, director, run, recipient_fields, notice.instance_id,
                               TurnOutcome.STOPPED, Path(), "", notice.reason, notice_type=TurnOutcome.STOPPED)
    row = record.by_id(notice.worker_id)
    if row is not None:
        completion = progress_history.hook_completion(passed)
        if isinstance(completion, progress_history.HookCompletion) and completion.stopped_reply == progress_history.StoppedReplyDisposition.SAVED:
            decision = _stopped_notice(row, director, run, passed, completion)
        if delivery.actions.finish_run:
            decision = _close_stopped_after_notice(row, director, run, notice.instance_id, decision, EventKind.RUN_FINISHED)
    return _detached_stop_completion(decision)


def stop_pass(run: Path, stopped: detached_runs.RunRecord,
              guard_ended: Callable[[], AbstractContextManager[worker_panes.CloseAllowed | worker_panes.Kept]],
              ) -> worker_panes.StoppedRunPassCompletion | worker_panes.Kept | worker_panes.NoOwningPanePass:
    """End the owning pass and prepare its director notice."""
    linked = stopped.get("pane_dispatch_id")
    if linked == "":
        return worker_panes.NoOwningPanePass.UNRECORDED
    if isinstance(linked, dict):
        return worker_panes.Kept(linked["unavailable"])
    notice = progress_history.stopped_run_notice(run, stopped["run_id"])
    if isinstance(notice, progress_history.StoppedPassNotice):
        return _recorded_stop_notice(run, notice)
    reason = stopped.get("stop_reason")
    if reason is None:
        return worker_panes.Kept("the stop reason is unavailable")
    record = staff.snapshot()
    row = record.by_id(staff.StaffId(stopped["staff_id"]))
    if row is None or row.kind != "worker":
        return worker_panes.NoOwningPanePass.UNRECORDED
    parent = record.parent_of(row)
    if parent is None:
        return worker_panes.Kept("the worker parent is unavailable")
    with worker_panes.worker_lock(parent.id, staff.WorkerName.parse(row.name), blocking=True) as hold:
        if isinstance(hold, worker_panes.Kept):
            return hold
        record = staff.snapshot()
        current = record.by_id(row.id)
        if current is None or not hold.owns(current) or not current.reports_to(parent.id):
            return worker_panes.Kept("the worker changed before the stop acquired its lock")
        notice = progress_history.stopped_run_notice(run, stopped["run_id"])
        if isinstance(notice, progress_history.StoppedPassNotice):
            return _recorded_stop_notice(run, notice)
        passed = worker_panes.worker_pass(current, run, record=record)
        instance = passed.get("instance_id")
        if linked is not None and linked != instance:
            return worker_panes.Kept("the linked pane pass is no longer current")
        if not passed:
            return worker_panes.NoOwningPanePass.UNRECORDED
        if linked is None:
            started = passed.get("started_at")
            if not isinstance(started, (int, float)):
                return worker_panes.Kept("the pane pass has no recorded start time")
            if stopped["started_at"] < started:
                return worker_panes.NoOwningPanePass.UNRECORDED
        completion = progress_history.hook_completion(passed)
        delivery = progress_history.dispatch_delivery(passed)
        if isinstance(completion, progress_history.HookCompletion) and completion.outcome == TurnOutcome.STOPPED:
            if passed.get("status") == "canceled" or (passed.get("status") == "interrupted"
                    and progress_history.stopped_notice_delivered(run, str(instance))):
                return _detached_stop_completion(_stopped_notice(current, parent, run, passed, completion))
        if (passed.get("status") == "completed" and isinstance(instance, str)
                and isinstance(completion, progress_history.HookCompletion) and completion.outcome == TurnOutcome.DONE):
            if completion.finished:
                return _detached_stop_completion(TurnDecision(TurnOutcome.DONE, _no_notice))
            state = worker_panes.observe_worker(parent.id, current, record)
            decision = _finish_completion(current, parent, run, passed, instance, delivery, completion, state)
            return _detached_stop_completion(decision)
        if passed.get("status") not in ("active", "refused"):
            return worker_panes.NoOwningPanePass.UNRECORDED
        dispatch = progress_history.active_dispatch(current.id, run)
        if isinstance(dispatch, progress_history.ActiveDispatchUnavailable):
            return worker_panes.Kept(dispatch.reason)
        if not isinstance(dispatch, progress_history.ActiveDispatch) or dispatch.instance_id != instance:
            return worker_panes.Kept("the open pane pass identity is unavailable")
        event = WorkerEvent(str(current.current_session), EventKind.RUN_FINISHED)
        with ExitStack() as admission:
            ended = admission.enter_context(guard_ended())
            if isinstance(ended, worker_panes.Kept):
                return ended
            receipt = _recover_pass(event, current, parent, run, passed, delivery, require_transcript=True)
            if isinstance(receipt, worker.DispatchEvidenceUnknown):
                return worker_panes.Kept(receipt.reason)
            if (isinstance(receipt, (worker.DispatchComplete, worker.DispatchWaitingForRuns, worker.DispatchAwaitingReply))
                    or receipt in (TurnOutcome.WAITING_ON_RUNS, TurnOutcome.WAITING_ON_REPLY)):
                return worker_panes.Kept("the pass has a reply pending completion")
            if not isinstance(receipt, progress_history.HookCompletion):
                slot = str(passed.get("team_slot", ""))
                notice = progress_history.stop_dispatch(run, slot, dispatch.instance_id, current, parent.id,
                                                        run_id=stopped["run_id"], reason=reason)
                if isinstance(notice, progress_history.StopDispatchRefused):
                    return worker_panes.Kept(notice.value)
                return _recorded_stop_notice(run, notice)
            if receipt.outcome != TurnOutcome.DONE:
                return worker_panes.Kept("the pane pass already has a completion outcome")
            state = worker_panes.observe_worker(parent.id, current, record)
            decision = _finish_completion(current, parent, run, passed, dispatch.instance_id, delivery, receipt, state,
                                          after_pass_recorded=admission.close)
        return _detached_stop_completion(decision)


def repeat_completion_notice(worker_name: str, parent: staff.StaffRow, run: Path,
                             passed: dict[str, object]) -> Callable[[], None]:
    """Repeat an explicitly requested result after the worker lock is released."""
    instance = str(passed["instance_id"])
    completion = progress_history.hook_completion(passed)
    if isinstance(completion, progress_history.HookCompletion) and not completion_notice_delivered(passed):
        def recover() -> None:
            name = staff.WorkerName.parse(worker_name)
            with worker_panes.worker_lock(parent.id, name, blocking=True) as hold:
                if isinstance(hold, worker_panes.Kept):
                    raise ValueError(hold.blocker)
                record = staff.snapshot()
                row = record.by_id(staff.StaffId(str(passed.get("worker_id", ""))))
                if row is None or worker_panes.worker_pass(row, run, record=record).get("instance_id") != instance:
                    return
                recovery = recover_completion(row, parent, run, hold, record=record)
                if recovery in (worker.CompletionRecovery.WAITING_ON_RUNS, worker.CompletionRecovery.WAITING_ON_REPLY,
                                worker.CompletionRecovery.PENDING_CLOSE):
                    raise ValueError(f"{worker_name}: {recovery.value}")

        return recover
    delivery = progress_history.dispatch_delivery(passed)
    outcome = TurnOutcome(completion.outcome) if isinstance(completion, progress_history.HookCompletion) else TurnOutcome.DONE
    path = Path(completion.reply_file if isinstance(completion, progress_history.HookCompletion) else delivery.reply_file)
    details = "\n".join(completion.details) if isinstance(completion, progress_history.HookCompletion) else ""
    reply = _completion_reply(completion) if isinstance(completion, progress_history.HookCompletion) else path.read_text()
    repeated = {key: value for key, value in passed.items() if key != "hook_notice"}
    decision = _prepare_notice(worker_name, parent, run, repeated, instance, outcome, path, reply, details, repeat=True)
    if delivery.actions.status_file:
        worker.CompletionArtifacts(parent, run).write(Path(delivery.actions.status_file), "done\n")

    def resend() -> None:
        _ = decision.send_notice()

    return resend


def _submit_stage(parent: staff.StaffRow, row: staff.StaffRow, state: worker_panes.WorkerPane,
                  client: herdr.Herdr, run: Path, passed: dict[str, object], instance: str,
                  delivery: progress_history.DispatchDelivery) -> TurnOutcome | TurnDecision:
    with redirect_stderr(io.StringIO()) as diagnostics:
        try:
            result = (worker.dispatch_turn(parent.id, state, client, delivery.line, delivery.completion_id, run=run)
                      if isinstance(state, worker_panes.OpenWorker) else worker.ExitCode.UNCERTAIN)
        except (OSError, herdr_types.HerdrFailure, subprocess.SubprocessError) as error:
            print(str(error), file=sys.stderr)
            result = worker.ExitCode.UNCERTAIN
    if result is worker.DispatchSubmission.SUBMITTED:
        progress_history.confirm_assignment_submission(run, instance, delivery.completion_id)
        return TurnOutcome.SUBMITTED
    outcome = TurnOutcome.BUSY if result == worker.ExitCode.BUSY else TurnOutcome.UNCERTAIN
    details = diagnostics.getvalue().strip() or f"Stage 2 submission returned {result}"
    path = Path(delivery.reply_file)
    path = path.with_name(path.stem + "_turn" + path.suffix)
    worker.CompletionArtifacts(parent, run).reply(path, details)
    return _prepare_notice(row.name, parent, run, passed, instance, outcome, path, details, details)


def _finish_completion(row: staff.StaffRow, parent: staff.StaffRow, run: Path, passed: dict[str, object],
                       instance: str, delivery: progress_history.DispatchDelivery,
                       completion: progress_history.HookCompletion,
                       state: worker_panes.WorkerPane | worker_panes.WorkerNotObserved, *,
                       pane: worker.CloseCompletedPane | worker.ReuseCompletedPane = worker.CloseCompletedPane.CLOSE,
                       lock: worker_panes.WorkerLockHold | worker_panes.WorkerLockAtUse = worker_panes.WorkerLockAtUse.ACQUIRE,
                       after_pass_recorded: Callable[[], None] = _no_notice,
                       ) -> TurnDecision:
    from scripts.direct import assign
    from scripts.message import send

    path = Path(completion.reply_file)
    if not completion.sender_native_id:
        sender = (send.ClaudeSender(str(row.current_session)) if isinstance(staff.backend(row), staff.ClaudeBackend)
                  else send.CodexSender(str(row.current_session)))
        completion = replace(completion, sender_native_id=send.sender_native_id(sender))
        progress_history.save_hook_completion(run, instance, completion)
    if not completion.actions_done:
        result = assign.complete_herdr(run, row, passed, delivery.actions, path, owner=parent,
                                      completed=completion.outcome == TurnOutcome.DONE)
        completion = replace(completion, actions_done=True)
        if isinstance(result, assign.CompletionFailed):
            completion = replace(completion, outcome=TurnOutcome.ERROR, details=(*completion.details, result.reason))
        progress_history.save_hook_completion(run, instance, completion)
    outcome = TurnOutcome(completion.outcome)
    if outcome != TurnOutcome.LATE_DONE and passed.get("status") in ("active", "refused"):
        progress_history.complete_dispatch(run, str(passed.get("team_slot", row.name)), instance,
                                           status="completed" if outcome == TurnOutcome.DONE else "error")
    after_pass_recorded()
    diagnostics: list[str] = []
    try:
        _ = scratch.end(scratch.worker_task(row.id))
    except Exception as error:
        diagnostics.append(f"scratch cleanup failed for {row.name}: {' '.join(str(error).splitlines())}")
    if diagnostics:
        details = tuple(dict.fromkeys((*completion.details, *diagnostics)))
        if details != completion.details:
            completion = replace(completion, details=details)
            progress_history.save_hook_completion(run, instance, completion)
    reply = _completion_reply(completion)

    notice_type = outcome.value
    if outcome == TurnOutcome.ERROR and worker_admission.reply_ends_dispatch(reply, delivery.completion_id):
        notice_type = TurnOutcome.DONE.value
    decision = _prepare_notice(row.name, parent, run, {**passed, "hook_completion": asdict(completion)},
                               instance, outcome, path, reply, "\n".join(completion.details), notice_type=notice_type)

    def finalize(hold: worker_panes.WorkerLockHold) -> TurnOutcome:
        record = staff.snapshot()
        member = record.by_id(row.id)
        if (member is None or member.name != row.name or member.current_session != row.current_session
                or not hold.owns(member) or worker_panes.worker_run(member, record=record) != run):
            return outcome
        current = worker_panes.worker_pass(member, run, record=record)
        saved = progress_history.hook_completion(current)
        if (current.get("instance_id") != instance or not isinstance(saved, progress_history.HookCompletion)
                or not _same_completion(saved, completion) or not completion_notice_delivered(current)):
            return outcome
        if saved.finished:
            return outcome
        return _finalize_completion(member, parent, run, instance, delivery, saved, state, hold, pane)

    def send_notice() -> TurnOutcome:
        _ = decision.send_notice()
        if isinstance(lock, worker_panes.WorkerLockHold):
            return finalize(lock)
        with worker_panes.worker_lock(parent.id, staff.WorkerName.parse(row.name), blocking=True) as hold:
            if isinstance(hold, worker_panes.Kept):
                raise ValueError(hold.blocker)
            return finalize(hold)

    return TurnDecision(outcome, send_notice)


def _finalize_completion(row: staff.StaffRow, parent: staff.StaffRow, run: Path, instance: str,
                         delivery: progress_history.DispatchDelivery, completion: progress_history.HookCompletion,
                         state: worker_panes.WorkerPane | worker_panes.WorkerNotObserved,
                         hold: worker_panes.WorkerLockHold, pane: worker.CloseCompletedPane | worker.ReuseCompletedPane,
                         ) -> TurnOutcome:
    from scripts.direct import assign

    details = list(completion.details)
    diagnostics: list[str] = []

    try:
        name = staff.WorkerName.parse(row.name)
        if (isinstance(pane, worker.ReuseCompletedPane) and not completion.role_released
                and delivery.actions.held_role == pane.held_role):
            completion = replace(completion, role_released=True)
            progress_history.save_hook_completion(run, instance, completion)
        if isinstance(pane, worker.ReuseCompletedPane) and not completion.pane_closed:
            if completion.outcome in (TurnOutcome.DONE, TurnOutcome.LATE_DONE):
                evidence = worker.completion_close_evidence(row, delivery, run=run, reply_file=completion.reply_file)
                if isinstance(evidence, worker_panes.Kept):
                    raise ValueError(evidence.blocker)
            completion = replace(completion, pane_closed=True)
            progress_history.save_hook_completion(run, instance, completion)
        if isinstance(pane, worker.CloseCompletedPane) and not completion.pane_closed:
            with redirect_stderr(io.StringIO()) as errors:
                if delivery.actions.end_worker:
                    ended = worker.end(parent.id, name, lock=hold, retire=False)
                    closed = worker_panes.Closed() if ended == worker.ExitCode.OK else worker_panes.Kept(
                        errors.getvalue().strip() or f"worker end returned {ended}")
                else:
                    closed = worker_panes.close(parent.id, name, lock=hold)
            if isinstance(closed, worker_panes.Closed):
                completion = replace(completion, pane_closed=True)
                progress_history.save_hook_completion(run, instance, completion)
            else:
                reason = closed.blocker if isinstance(closed, worker_panes.Kept) else closed.reason
                diagnostics.append(_kept_pane_anomaly(row, state, reason))
        if completion.pane_closed and not completion.role_released:
            if delivery.actions.held_role:
                from scripts.astra import astra_roles
                astra_roles.release(delivery.actions.held_role)
            completion = replace(completion, role_released=True)
            progress_history.save_hook_completion(run, instance, completion)
    except (OSError, ValueError, herdr_types.HerdrFailure, subprocess.SubprocessError,
            staff.StaffRefused, staff.StaffUnreadable) as error:
        diagnostics.append(_kept_pane_anomaly(row, state, str(error)))
    if completion.pane_closed:
        try:
            _ = detached_runs.prune_finished(run / detached_runs.DETACHED_RUNS_DIR)
        except OSError as error:
            diagnostics.append(f"detached run cleanup failed for {row.name}: {' '.join(str(error).splitlines())}")
    outcome = TurnOutcome(completion.outcome)
    artifacts = worker.CompletionArtifacts(parent, run)
    stopped = outcome == TurnOutcome.STOPPED
    needs_recording_after_close = stopped or delivery.actions.finish_run and artifacts.protected
    closed = completion.pane_closed and completion.role_released
    if not completion.recording_done and (closed or not needs_recording_after_close):
        status = "stopped" if stopped else "completed" if outcome == TurnOutcome.DONE and completion.pane_closed else "error"
        result = assign.finish_herdr_recording(run, instance, delivery.actions, status=status)
        if isinstance(result, assign.CompletionFailed):
            diagnostics.append(result.reason if outcome == TurnOutcome.DONE else f"{outcome}: {result.reason}")
            if not stopped:
                outcome = TurnOutcome.ERROR
        if not needs_recording_after_close or isinstance(result, assign.CompletionApplied):
            completion = replace(completion, recording_done=True)
    diagnostic_path = _completion_diagnostics_path(run, instance)
    recorded = _record_completion_diagnostics(artifacts, diagnostic_path, diagnostics)
    new_details = tuple(dict.fromkeys((*details, *diagnostics)))
    completion = replace(completion, details=new_details)
    progress_history.save_hook_completion(run, instance, completion)
    _deliver_completion_diagnostics(row, parent, run, instance, diagnostic_path, recorded,
                                    sender_native_id=completion.sender_native_id)
    if closed and completion.recording_done:
        progress_history.save_hook_completion(run, instance, replace(completion, finished=True))
        if isinstance(pane, worker.CloseCompletedPane):
            _remove_completed_restart(row, delivery)
    return outcome


def _remove_completed_restart(row: staff.StaffRow, delivery: progress_history.DispatchDelivery) -> None:
    if delivery.actions.end_worker:
        _ = staff.retire(row.id)
    shutdown_record.forget([str(row.id)])


def _completion_diagnostics_path(run: Path, instance: str) -> Path:
    digest = hashlib.sha256(instance.encode()).hexdigest()
    return run / f".{digest}.completion-diagnostics.json"


def _record_completion_diagnostics(artifacts: worker.CompletionArtifacts, path: Path,
                                   messages: Iterable[str]) -> dict[str, object]:
    recorded = worker_lifecycle.read_json_object(path)
    count = len(recorded)
    for message in messages:
        _ = recorded.setdefault(message, False)
    if len(recorded) != count:
        artifacts.write(path, json.dumps(recorded))
    return recorded


def _deliver_completion_diagnostics(row: staff.StaffRow, parent: staff.StaffRow, run: Path, instance: str,
                                    path: Path, recorded: dict[str, object], *, sender_native_id: str = "") -> None:
    from scripts.message import send

    artifacts = worker.CompletionArtifacts(parent, run)
    for message, delivered in recorded.items():
        if delivered is True:
            continue
        current = staff.snapshot().by_id(parent.id)
        if current is None:
            raise ValueError(f"{parent.id}: notice recipient is not in staff")
        address = ("codex:" if isinstance(staff.backend(current), staff.CodexBackend) else "session:") + str(current.current_session)
        digest = hashlib.sha256(message.encode()).hexdigest()
        options = send.parse(["--to", address, "--from", row.name, "--text", message,
            "--key", f"worker-completion:{instance}:diagnostic:{digest}", "--recipient-key", f"staff:{parent.id}",
            "--repeat-minutes", str(COMPLETION_NOTICE_REPEAT_MINUTES)])
        if sender_native_id:
            sender = send.read_sender_stamp(sender_native_id)
        else:
            identity = str(row.current_session)
            sender = send.ClaudeSender(identity) if isinstance(staff.backend(row), staff.ClaudeBackend) else send.CodexSender(identity)
        result = send.send(options._replace(
            sender_origin=send.ForwardedSender(sender),
            shown_sender=send.ShownSender(row.name)))
        if result.outcome not in ("sent", "skipped"):
            raise ValueError(result.detail)
        recorded[message] = True
        artifacts.write(path, json.dumps(recorded))


def recover_completion(row: staff.StaffRow, parent: staff.StaffRow, run: Path,
                       hold: worker_panes.WorkerLockHold, *, record: staff.StaffSnapshot,
                       pane: worker.CloseCompletedPane | worker.ReuseCompletedPane | worker.KeepCompletedPane = worker.CloseCompletedPane.CLOSE,
                       ) -> worker.CompletionRecovery:
    """Deliver the current pass notice before restore closes or dispatch reuses its pane."""
    passed = worker_panes.worker_pass(row, run, record=record)
    instance = str(passed.get("instance_id", ""))
    if not instance:
        return worker.CompletionRecovery.NO_PENDING_NOTICE
    stopped = progress_history.hook_completion(passed)
    if isinstance(stopped, progress_history.HookCompletion) and stopped.outcome == TurnOutcome.STOPPED:
        if stopped.finished:
            return worker.CompletionRecovery.NO_PENDING_NOTICE
        if passed.get("status") not in ("canceled", "interrupted") or isinstance(pane, worker.KeepCompletedPane):
            return worker.CompletionRecovery.NO_PENDING_NOTICE
        receipt = progress_history.stopped_pass_receipt(run, passed)
        recovered = _record_stopped_reply(row, parent, run, receipt, stopped)
        if isinstance(recovered, worker.DispatchEvidenceUnknown):
            raise ValueError(recovered.reason)
        state = worker_panes.observe_worker(parent.id, row, record)
        if isinstance(recovered, StoppedReplyRecovery):
            recovery = _stopped_reply_wait(recovered, state, receipt, pane)
            if isinstance(recovery, worker.CompletionRecovery):
                return recovery
        else:
            stopped = recovered
        _ = _stopped_notice(row, parent, run, passed, stopped).send_notice()
        return _finalize_stopped(row, parent, run, instance, progress_history.dispatch_delivery(passed), receipt,
                                 stopped, state, hold, pane)
    delivery = progress_history.dispatch_delivery(passed)
    event = WorkerEvent(str(row.current_session), EventKind.RUN_FINISHED)
    completion = _recover_pass(event, row, parent, run, passed, delivery, require_transcript=True)
    if isinstance(completion, worker.DispatchEvidenceUnknown):
        raise ValueError(completion.reason)
    if completion == TurnOutcome.WAITING_ON_RUNS:
        return worker.CompletionRecovery.WAITING_ON_RUNS
    if completion == TurnOutcome.WAITING_ON_REPLY or isinstance(completion, worker.DispatchAwaitingReply):
        return worker.CompletionRecovery.WAITING_ON_REPLY
    if isinstance(completion, worker.DispatchWaitingForRuns) and not completion.has_live_run:
        return worker.CompletionRecovery.WAITING_ON_REPLY
    if (isinstance(completion, worker.DispatchWaitingForRuns) and completion.has_live_run
            and isinstance(completion.reply, (worker.DispatchComplete, worker.DispatchAwaitingReply))):
        return worker.CompletionRecovery.WAITING_ON_RUNS
    if completion == TurnOutcome.IGNORED:
        raise ValueError(f"{row.name}: the saved DONE has no accepted dispatch receipt")
    if not isinstance(completion, progress_history.HookCompletion) or isinstance(pane, worker.KeepCompletedPane):
        return worker.CompletionRecovery.NO_PENDING_NOTICE
    state = worker_panes.observe_worker(parent.id, row, record)
    if completion.finished:
        if isinstance(pane, worker.CloseCompletedPane) and isinstance(state, worker_panes.ClosedWorker):
            _remove_completed_restart(row, delivery)
        return worker.CompletionRecovery.NO_PENDING_NOTICE
    if isinstance(pane, worker.ReuseCompletedPane):
        late = f"late result of pass {instance} in run {run.name}"
        if late not in completion.details:
            completion = replace(completion, details=(*completion.details, late))
            progress_history.save_hook_completion(run, instance, completion)
    _ = _finish_completion(row, parent, run, passed, instance, delivery, completion, state,
                           pane=pane, lock=hold).send_notice()
    saved = progress_history.hook_completion(worker_panes.worker_pass(row, run, record=record))
    if isinstance(saved, progress_history.HookCompletion) and saved.finished:
        return worker.CompletionRecovery.RECOVERED
    return worker.CompletionRecovery.PENDING_CLOSE


def _log_hook_event(event: WorkerEvent, reason: str, worker_name: str) -> None:
    path = herdr.layout_path(herdr.shared_session()).parent / "turn-end-ignored.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        _ = stream.write(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(),
            "reason": reason, "thread_id": event.thread_id, "worker_name": worker_name,
            "event": event.kind}) + "\n")


def _ignored(event: WorkerEvent, reason: str, worker_name: str = "unknown") -> TurnOutcome:
    """Save each ignored event for the director without sending a notice."""
    _log_hook_event(event, reason, worker_name)
    return TurnOutcome.IGNORED


class WorkerByThread(Enum):
    LOOKUP = "resolve the worker from the hook thread"


def handle(event: WorkerEvent, *, owner: staff.StaffId | WorkerByThread = WorkerByThread.LOOKUP) -> TurnOutcome:
    """Send the decided notice after releasing the worker lock."""
    try:
        decision = _handle_locked(event, owner)
        if isinstance(decision, TurnDecision):
            return decision.send_notice() or decision.outcome
    except recorder_state.OldRecorderState as error:
        print(error, file=sys.stderr)
        return TurnOutcome.UNCERTAIN
    return decision


class NoLateDone(Enum):
    UNRECORDED = "no late DONE for the recorded pass"


def _late_reply_path(row: staff.StaffRow, run: Path, reply_file: str, instance: str) -> Path:
    destination = Path(reply_file) if reply_file else run / f"{row.name}_reply.txt"
    return destination.with_name(f"{destination.stem}_late_{instance}{destination.suffix}")


class StoppedReplyRecovery(Enum):
    SAVED = "saved the verified reply"
    ALREADY_SAVED = "the file contains the latest verified reply"
    UNVERIFIED = "no verified DONE for the stopped pass"
    WAITING_ON_RUNS = worker.CompletionRecovery.WAITING_ON_RUNS.value
    WAITING_ON_REPLY = worker.CompletionRecovery.WAITING_ON_REPLY.value


class StoppedReplyCanEnd(Enum):
    ENDED = "the stopped pass no longer needs a reply"


class StoppedPassPane(Enum):
    CURRENT = "this pass still owns the worker pane"
    REPLACED = "the current pane belongs to newer work"


def _save_stopped_reply(row: staff.StaffRow, parent: staff.StaffRow, run: Path,
                        receipt: progress_history.StoppedPassReceipt,
                        ) -> StoppedReplyRecovery | worker.DispatchEvidenceUnknown:
    if not receipt.line:
        if detached_runs.live_runs(run / detached_runs.DETACHED_RUNS_DIR, row.name, identity=row.id):
            return StoppedReplyRecovery.WAITING_ON_RUNS
        return StoppedReplyRecovery.UNVERIFIED
    scope = (worker_panes.PassDetachedResults(receipt.instance_id, receipt.results.pass_started_at,
                                            live_runs_checked=False)
             if isinstance(receipt.results, progress_history.LegacyResultsForStoppedPass) else receipt)
    reply = worker.dispatch_receipt(row, receipt.line, receipt.completion_id, run=run,
                                    require_transcript=True, pane_pass=scope)
    if isinstance(reply, worker.DispatchEvidenceUnknown):
        return reply
    if isinstance(reply, worker.DispatchWaitingForRuns):
        return StoppedReplyRecovery.WAITING_ON_RUNS if reply.has_live_run else StoppedReplyRecovery.WAITING_ON_REPLY
    if isinstance(reply, worker.DispatchAwaitingReply):
        return StoppedReplyRecovery.WAITING_ON_REPLY
    if not isinstance(reply, worker.DispatchComplete):
        return StoppedReplyRecovery.UNVERIFIED
    artifacts = worker.CompletionArtifacts(parent, run)
    path = artifacts.authorized_path(_late_reply_path(row, run, receipt.reply_file, receipt.instance_id))
    try:
        if path.read_text(encoding="utf-8").rstrip() == reply.reply.rstrip():
            return StoppedReplyRecovery.ALREADY_SAVED
    except FileNotFoundError:
        pass
    artifacts.reply(path, reply.reply)
    return StoppedReplyRecovery.SAVED


def _record_stopped_reply(row: staff.StaffRow, parent: staff.StaffRow, run: Path, receipt: progress_history.StoppedPassReceipt,
                          completion: progress_history.HookCompletion,
                          ) -> progress_history.HookCompletion | StoppedReplyRecovery | worker.DispatchEvidenceUnknown:
    if completion.finished or completion.stopped_reply == progress_history.StoppedReplyDisposition.ENDED_WITHOUT_DONE:
        return completion
    if completion.pane_closed and completion.reply_file:
        recovered = StoppedReplyRecovery.ALREADY_SAVED
    else:
        recovered = _save_stopped_reply(row, parent, run, receipt)
    if recovered not in (StoppedReplyRecovery.SAVED, StoppedReplyRecovery.ALREADY_SAVED):
        return recovered
    path = _late_reply_path(row, run, receipt.reply_file, receipt.instance_id)
    saved = replace(completion, reply_file=str(path), stopped_reply=progress_history.StoppedReplyDisposition.SAVED)
    if saved != completion:
        progress_history.save_hook_completion(run, receipt.instance_id, saved)
    return saved


def _stopped_reply_wait(recovery: StoppedReplyRecovery, state: worker_panes.WorkerPane | worker_panes.WorkerNotObserved,
                        receipt: progress_history.StoppedPassReceipt,
                        pane: worker.CloseCompletedPane | worker.ReuseCompletedPane,
                        ownership: StoppedPassPane = StoppedPassPane.CURRENT,
                        ) -> worker.CompletionRecovery | StoppedReplyCanEnd:
    if recovery == StoppedReplyRecovery.WAITING_ON_RUNS:
        return worker.CompletionRecovery.WAITING_ON_RUNS
    if ownership is StoppedPassPane.REPLACED:
        return StoppedReplyCanEnd.ENDED
    if isinstance(state, (worker_panes.StoppedWorker, worker_panes.ClosedWorker)):
        return StoppedReplyCanEnd.ENDED
    if isinstance(state, worker_panes.OpenWorker) and state.status.idle:
        if recovery == StoppedReplyRecovery.UNVERIFIED or (isinstance(pane, worker.ReuseCompletedPane)
                and isinstance(receipt.results, progress_history.LinkedResultsForStoppedPass)):
            return StoppedReplyCanEnd.ENDED
    return worker.CompletionRecovery.WAITING_ON_REPLY


def _finalize_stopped(row: staff.StaffRow, parent: staff.StaffRow, run: Path, instance: str,
                      delivery: progress_history.DispatchDelivery, receipt: progress_history.StoppedPassReceipt,
                      completion: progress_history.HookCompletion,
                      state: worker_panes.WorkerPane | worker_panes.WorkerNotObserved,
                      hold: worker_panes.WorkerLockHold, pane: worker.CloseCompletedPane | worker.ReuseCompletedPane, *,
                      report: Callable[[str], None] | None = None,
                      ownership: StoppedPassPane = StoppedPassPane.CURRENT) -> worker.CompletionRecovery:
    if completion.finished:
        return worker.CompletionRecovery.NO_PENDING_NOTICE
    if not progress_history.stopped_notice_delivered(run, instance):
        return worker.CompletionRecovery.PENDING_CLOSE
    recovered = _record_stopped_reply(row, parent, run, receipt, completion)
    if isinstance(recovered, worker.DispatchEvidenceUnknown):
        raise ValueError(recovered.reason)
    if isinstance(recovered, StoppedReplyRecovery):
        recovery = _stopped_reply_wait(recovered, state, receipt, pane, ownership)
        if isinstance(recovery, worker.CompletionRecovery):
            return recovery
        completion = replace(completion, stopped_reply=progress_history.StoppedReplyDisposition.ENDED_WITHOUT_DONE)
        progress_history.save_hook_completion(run, instance, completion)
    else:
        completion = recovered
    if not completion.pane_closed and isinstance(pane, worker.CloseCompletedPane):
        name = staff.WorkerName.parse(row.name)
        try:
            with redirect_stderr(io.StringIO()):
                closed = worker_panes.close(parent.id, name, lock=hold)
        except (OSError, ValueError, herdr_types.HerdrFailure, subprocess.SubprocessError,
                staff.StaffRefused, staff.StaffUnreadable) as error:
            if report is None:
                raise
            report(str(error))
            return worker.CompletionRecovery.PENDING_CLOSE
        if not isinstance(closed, worker_panes.Closed):
            reason = closed.blocker if isinstance(closed, worker_panes.Kept) else closed.reason
            if report is None:
                raise ValueError(reason)
            report(reason)
            return worker.CompletionRecovery.PENDING_CLOSE
        completion = replace(completion, pane_closed=True)
        progress_history.save_hook_completion(run, instance, completion)
    _ = _finalize_completion(row, parent, run, instance, delivery, completion, state, hold, pane)
    saved = progress_history.hook_completion(progress_history.pass_for_finalization(run, instance))
    if isinstance(saved, progress_history.HookCompletion) and saved.finished:
        return worker.CompletionRecovery.RECOVERED
    return worker.CompletionRecovery.PENDING_CLOSE


def _close_stopped_after_notice(row: staff.StaffRow, parent: staff.StaffRow, run: Path, instance: str,
                                decision: TurnDecision, event_kind: EventKind) -> TurnDecision:
    def report(reason: str) -> None:
        message = f"{row.name}: kept pane: {' '.join(reason.splitlines())}"
        print(message, file=sys.stderr)
        _send_stopped_diagnostics(row, parent, run, instance, (message,))

    def send_notice() -> TurnOutcome | None:
        _ = decision.send_notice()
        name = staff.WorkerName.parse(row.name)
        with ExitStack() as after_unlock, worker_panes.worker_lock(parent.id, name, blocking=True) as hold:
            if isinstance(hold, worker_panes.Kept):
                _ = after_unlock.callback(report, hold.blocker)
                return TurnOutcome.RETRY_EVIDENCE if event_kind == EventKind.RUN_FINISHED else None
            record = staff.snapshot()
            current = record.by_id(row.id)
            if current is None or not current.reports_to(parent.id) or not hold.owns(current):
                return TurnOutcome.RETRY_EVIDENCE if event_kind == EventKind.RUN_FINISHED else None
            passed = progress_history.pass_for_finalization(run, instance)
            saved = progress_history.hook_completion(passed)
            if (passed.get("worker_id") != current.id or passed.get("status") not in ("canceled", "interrupted")
                    or not isinstance(saved, progress_history.HookCompletion) or saved.outcome != TurnOutcome.STOPPED):
                return None
            state = worker_panes.observe_worker(parent.id, current, record)
            receipt = progress_history.stopped_pass_receipt(run, passed)
            delivery = progress_history.dispatch_delivery(passed)
            pane, ownership = _stopped_pane_use(current, run, receipt, delivery, record)
            def report_after_unlock(reason: str) -> None:
                _ = after_unlock.callback(report, reason)

            try:
                recovery = _finalize_stopped(current, parent, run, instance, delivery, receipt, saved, state, hold, pane,
                                             report=report_after_unlock, ownership=ownership)
            except ValueError as error:
                _ = after_unlock.callback(report, str(error))
                return TurnOutcome.RETRY_EVIDENCE if event_kind == EventKind.RUN_FINISHED else None
            if event_kind == EventKind.RUN_FINISHED and recovery in (
                    worker.CompletionRecovery.PENDING_CLOSE, worker.CompletionRecovery.WAITING_ON_RUNS,
                    worker.CompletionRecovery.WAITING_ON_REPLY):
                return TurnOutcome.RETRY_EVIDENCE
            return None

    return replace(decision, send_notice=send_notice)


def _stopped_pane_use(row: staff.StaffRow, run: Path, receipt: progress_history.StoppedPassReceipt,
                      delivery: progress_history.DispatchDelivery,
                      record: staff.StaffSnapshot,
                      ) -> tuple[worker.CloseCompletedPane | worker.ReuseCompletedPane, StoppedPassPane]:
    current_run = worker_panes.worker_run(row, record=record)
    current = worker_panes.worker_pass(row, current_run, record=record)
    if ((not receipt.worker_session or receipt.worker_session == row.current_session)
            and current_run.resolve() == run.resolve() and current.get("instance_id") == receipt.instance_id):
        return worker.CloseCompletedPane.CLOSE, StoppedPassPane.CURRENT
    # A newer pass can hold the same Astra role key for this worker.
    held = progress_history.dispatch_delivery(current).actions.held_role if current else delivery.actions.held_role
    return worker.ReuseCompletedPane(held), StoppedPassPane.REPLACED


def recover_stopped_in_run(run: Path, identity: staff.StaffId, *,
                           hold: worker_panes.WorkerLockHold | worker_panes.WorkerLockAtUse = worker_panes.WorkerLockAtUse.ACQUIRE,
                           ) -> worker.CompletionRecovery:
    """Complete stopped passes by instance under the worker lock."""
    if not tuple(progress_history.stopped_pass_receipts(run, identity)):
        return worker.CompletionRecovery.NO_PENDING_NOTICE
    record = staff.snapshot()
    row = record.by_id(identity)
    parent = record.parent_of(row) if row is not None else None
    if row is None or parent is None:
        return worker.CompletionRecovery.PENDING_CLOSE
    with ExitStack() as operations:
        locked = (operations.enter_context(worker_panes.worker_lock(parent.id, staff.WorkerName.parse(row.name)))
                  if isinstance(hold, worker_panes.WorkerLockAtUse) else hold)
        if isinstance(locked, worker_panes.Kept):
            return worker.CompletionRecovery.PENDING_CLOSE
        record = staff.snapshot()
        current = record.by_id(identity)
        if current is None or not locked.owns(current) or not current.reports_to(parent.id):
            return worker.CompletionRecovery.PENDING_CLOSE
        recovery = worker.CompletionRecovery.NO_PENDING_NOTICE
        for receipt in progress_history.stopped_pass_receipts(run, identity):
            passed = progress_history.pass_for_finalization(run, receipt.instance_id)
            completion = progress_history.hook_completion(passed)
            if (not isinstance(completion, progress_history.HookCompletion) or completion.finished
                    or completion.outcome != TurnOutcome.STOPPED):
                continue
            delivery = progress_history.dispatch_delivery(passed)
            pane, ownership = _stopped_pane_use(current, run, receipt, delivery, record)
            state = worker_panes.observe_worker(parent.id, current, record)
            if (ownership is StoppedPassPane.CURRENT and not completion.pane_closed
                    and isinstance(state, worker_panes.OpenWorker) and not state.status.idle):
                return worker.CompletionRecovery.WAITING_ON_REPLY
            prepared = _record_stopped_reply(current, parent, run, receipt, completion)
            if isinstance(prepared, progress_history.HookCompletion):
                completion = prepared
            elif isinstance(prepared, worker.DispatchEvidenceUnknown):
                raise ValueError(prepared.reason)
            elif isinstance(wait := _stopped_reply_wait(prepared, state, receipt, pane, ownership),
                            worker.CompletionRecovery):
                return wait
            _ = _stopped_notice(current, parent, run, passed, completion).send_notice()
            recovery = _finalize_stopped(current, parent, run, receipt.instance_id, delivery, receipt,
                                         completion, state, locked, pane, ownership=ownership)
            if recovery != worker.CompletionRecovery.RECOVERED:
                return recovery
        return recovery


def _send_stopped_diagnostics(row: staff.StaffRow, parent: staff.StaffRow, run: Path, instance: str,
                              messages: tuple[str, ...] = (), *, sender_native_id: str = "") -> None:
    path = _completion_diagnostics_path(run, instance)
    if not messages and not path.exists():
        return
    digest = hashlib.sha256(instance.encode()).hexdigest()
    artifacts = worker.CompletionArtifacts(parent, run)
    with artifacts.lock(run / f".{digest}.notice.lock"):
        recorded = _record_completion_diagnostics(artifacts, path, messages)
        _deliver_completion_diagnostics(row, parent, run, instance, path, recorded, sender_native_id=sender_native_id)


def _stopped_pass_folders(parent: staff.StaffRow, record: staff.StaffSnapshot, run: Path) -> Generator[Path]:
    yield run
    held = unit_runs.held(str(parent.current_session), record=record, scanned=worker_panes.parent_run_index(record))
    recorded = (parent.run.path,) if isinstance(parent.run, staff.OpenRun) else ()
    fallback = worker_panes.worker_fallback_run(parent, record=record)
    for directory in dict.fromkeys((*(Path(folder) for folder in held), *recorded, fallback)):
        if directory != run:
            yield directory


def _recover_replaced_stops(event: WorkerEvent, row: staff.StaffRow, parent: staff.StaffRow, run: Path,
                            instance: str, marker: str,
                            archived_stops: Iterable[tuple[Path, progress_history.StoppedPassReceipt]],
                            after_unlock: ExitStack,
                            ) -> TurnOutcome | NoLateDone:
    recovered = False
    for directory, receipt in archived_stops:
        if directory == run and receipt.instance_id == instance:
            continue
        if marker and receipt.completion_id != marker:
            continue
        passed = progress_history.pass_for_finalization(directory, receipt.instance_id)
        completion = progress_history.hook_completion(passed)
        path = _late_reply_path(row, directory, receipt.reply_file, receipt.instance_id)
        try:
            saved = False
            if not marker and path.exists():
                saved = worker_admission.reply_ends_dispatch(path.read_text(encoding="utf-8"), receipt.completion_id)
            result = StoppedReplyRecovery.ALREADY_SAVED if saved else _save_stopped_reply(row, parent, directory, receipt)
        except (OSError, UnicodeError) as error:
            reason = " ".join(f"anomaly: unable to recover stopped pass reply {path}: {error}".splitlines())
            unit_runs.report_anomaly(reason)
            _log_hook_event(event, reason, row.name)
            continue
        if (isinstance(completion, progress_history.HookCompletion) and not completion.finished
                and completion.outcome == TurnOutcome.STOPPED
                and result in (StoppedReplyRecovery.SAVED, StoppedReplyRecovery.ALREADY_SAVED,
                               StoppedReplyRecovery.UNVERIFIED)):
            if result in (StoppedReplyRecovery.SAVED, StoppedReplyRecovery.ALREADY_SAVED):
                completion = replace(completion, reply_file=str(path), stopped_reply=progress_history.StoppedReplyDisposition.SAVED)
                progress_history.save_hook_completion(directory, receipt.instance_id, completion)
            decision = _stopped_notice(row, parent, directory, passed, completion)
            finalize = _close_stopped_after_notice(row, parent, directory, receipt.instance_id, decision, event.kind)
            _ = after_unlock.callback(finalize.send_notice)
        if marker:
            if result not in (StoppedReplyRecovery.SAVED, StoppedReplyRecovery.ALREADY_SAVED):
                return _ignored(event, "the stopped pass reply has no verified DONE receipt", row.name)
            return TurnOutcome.STOPPED
        recovered |= result == StoppedReplyRecovery.SAVED
    return TurnOutcome.STOPPED if recovered else NoLateDone.UNRECORDED


def _completion_receipt(event: WorkerEvent, row: staff.StaffRow, run: Path,
                        delivery: progress_history.DispatchDelivery, *, require_transcript: bool
                        ) -> worker.DispatchReceipt:
    receipt = worker.dispatch_receipt(row, delivery.line, delivery.completion_id, run=run,
                                      require_transcript=require_transcript)
    if (event.kind == EventKind.TURN_END and isinstance(receipt, worker.DispatchSubmitted)
            and worker_admission.reply_ends_dispatch(event.reply, delivery.completion_id)):
        return worker.DispatchComplete(event.reply)
    return receipt


def _save_late_done(event: WorkerEvent, row: staff.StaffRow, artifacts: worker.CompletionArtifacts,
                    passed: dict[str, object],
                    completion: progress_history.HookCompletion | progress_history.NoHookCompletion,
                    delivery: progress_history.DispatchDelivery, *, require_transcript: bool,
                    ) -> progress_history.HookCompletion | worker.DispatchEvidenceUnknown | TurnOutcome | NoLateDone:
    """Save the late reply and notice outcome for a pass the recorder already ended."""
    run = artifacts.run
    earlier = passed.get("status")
    instance = passed.get("instance_id")
    if earlier not in ("completed", "error", "canceled", "interrupted") or not isinstance(instance, str) or not instance:
        return NoLateDone.UNRECORDED
    if not delivery.line or event.kind == EventKind.STAGE_READY:
        return NoLateDone.UNRECORDED
    delivery = replace(delivery, completion_id=delivery.completion_id or instance)
    if event.kind == EventKind.RUN_FINISHED:
        try:
            saved_reply = _late_reply_path(row, run, delivery.reply_file, instance).read_text().rstrip()
        except FileNotFoundError:
            pass
        else:
            event = WorkerEvent(event.thread_id, reply=saved_reply)
    matching_event = (event.kind == EventKind.TURN_END
                      and worker_admission.reply_ends_dispatch(event.reply, delivery.completion_id))
    receipt = _completion_receipt(event, row, run, delivery, require_transcript=require_transcript)
    if isinstance(receipt, worker.DispatchEvidenceUnknown):
        return receipt
    if isinstance(receipt, worker.DispatchWaitingForRuns) and receipt.has_live_run:
        if matching_event or isinstance(receipt.reply, (worker.DispatchComplete, worker.DispatchAwaitingReply)):
            return TurnOutcome.WAITING_ON_RUNS
        return NoLateDone.UNRECORDED
    if isinstance(receipt, (worker.DispatchWaitingForRuns, worker.DispatchAwaitingReply)):
        return TurnOutcome.WAITING_ON_REPLY
    if not isinstance(receipt, worker.DispatchComplete):
        return TurnOutcome.IGNORED if matching_event else NoLateDone.UNRECORDED

    path = _late_reply_path(row, run, delivery.reply_file, instance)
    artifacts.reply(path, receipt.reply)
    details = (f"The earlier pass ended with state {earlier}.", *receipt.anomalies)
    if earlier == "error" and (not isinstance(completion, progress_history.HookCompletion) or not completion.finished):
        if isinstance(completion, progress_history.HookCompletion):
            details += completion.details
        if delivery.reply_file:
            path = Path(delivery.reply_file)
            artifacts.reply(path, receipt.reply)
        saved = progress_history.HookCompletion(TurnOutcome.DONE, str(path), details)
    else:
        if earlier == "completed" and delivery.reply_file:
            destination = Path(delivery.reply_file)
            try:
                recorded_reply = destination.read_text()
            except FileNotFoundError:
                artifacts.reply(destination, receipt.reply)
                path = destination
            else:
                if recorded_reply.rstrip() == receipt.reply.rstrip():
                    path = destination
        saved = progress_history.HookCompletion(TurnOutcome.LATE_DONE, str(path), details,
                                                actions_done=True, recording_done=True)
    progress_history.save_hook_completion(run, instance, saved)
    return saved


def _recover_pass(event: WorkerEvent, row: staff.StaffRow, parent: staff.StaffRow, run: Path, passed: dict[str, object],
                  delivery: progress_history.DispatchDelivery, *, require_transcript: bool = False,
                  ) -> progress_history.HookCompletion | progress_history.NoHookCompletion | worker.DispatchReceipt | TurnOutcome:
    """Select the saved completion or record a matching DONE under the worker lock."""
    artifacts = worker.CompletionArtifacts(parent, run)
    completion = progress_history.hook_completion(passed)
    if isinstance(completion, progress_history.HookCompletion):
        done = completion.outcome in (TurnOutcome.DONE, TurnOutcome.LATE_DONE)
        error_with_done = (completion.outcome == TurnOutcome.ERROR and completion.reply_file
                           and worker_admission.reply_ends_dispatch(_completion_reply(completion), delivery.completion_id))
        if done or error_with_done:
            if not delivery.line or done and completion.finished:
                return completion
            receipt = _completion_receipt(event, row, run, delivery, require_transcript=require_transcript)
            if isinstance(receipt, worker.DispatchWaitingForRuns):
                return TurnOutcome.WAITING_ON_RUNS if receipt.has_live_run else TurnOutcome.WAITING_ON_REPLY
            if isinstance(receipt, worker.DispatchAwaitingReply):
                return TurnOutcome.WAITING_ON_REPLY
            if isinstance(receipt, worker.DispatchEvidenceUnknown):
                return receipt
            if isinstance(receipt, worker.DispatchComplete):
                if completion_notice_delivered(passed):
                    stamp = _completion_notice_stamp(TurnOutcome(completion.outcome))
                    if passed.get("hook_notice") != stamp:
                        progress_history.record_dispatch_notice(run, str(passed["instance_id"]), stamp)
                        passed["hook_notice"] = stamp
                artifacts.reply(Path(completion.reply_file), receipt.reply)
            return completion
    late = _save_late_done(event, row, artifacts, passed, completion, delivery, require_transcript=require_transcript)
    if not isinstance(late, NoLateDone):
        return late
    if (isinstance(completion, progress_history.HookCompletion)
            or passed.get("status") not in ("active", "refused") or not delivery.line):
        return completion

    instance = str(passed["instance_id"])
    receipt = _completion_receipt(event, row, run, delivery, require_transcript=require_transcript)
    if (not isinstance(receipt, worker.DispatchComplete)
            or event.kind == EventKind.TURN_END and receipt.reply != event.reply and not receipt.restored_done
            or delivery.early_ready and delivery.completion_id == instance):
        return receipt
    path = Path(delivery.reply_file)
    artifacts.reply(path, receipt.reply)
    completion = progress_history.HookCompletion(TurnOutcome.DONE, str(path), receipt.anomalies)
    progress_history.save_hook_completion(run, instance, completion)
    return completion


def _done_marker(reply: str) -> str:
    lines = reply.rstrip().splitlines()
    marker = re.fullmatch(r"DONE ([A-Za-z0-9_-]+)\.?", lines[-1]) if lines else None
    return marker[1] if marker is not None else ""


def _stale_done_detail(reply: str, expected: str) -> str:
    marker = _done_marker(reply)
    if not expected or not marker or marker == expected:
        return ""
    return f"no DONE for pass {expected}; the reply ends with DONE {marker} from another pass"


def _handle_locked(event: WorkerEvent,
                   owner: staff.StaffId | WorkerByThread = WorkerByThread.LOOKUP) -> TurnOutcome | TurnDecision:
    """Recover completion records under the worker lock."""
    record = staff.snapshot()
    row = (record.by_current_session(event.thread_id) if isinstance(owner, WorkerByThread)
           else record.by_id(owner))
    if row is None:
        if not isinstance(owner, WorkerByThread):
            return TurnOutcome.RETRY_EVIDENCE
        return _ignored(event, "no staff row for the current thread")
    if row.kind != "worker":
        return _ignored(event, "the staff row is not a worker", row.name)
    parent = record.parent_of(row)
    if parent is None:
        return _ignored(event, "the worker has no parent", row.name)
    with ExitStack() as after_unlock, worker_panes.worker_lock(
            parent.id, staff.WorkerName.parse(row.name), blocking=True) as hold:
        if isinstance(hold, worker_panes.Kept):
            if event.kind == EventKind.RUN_FINISHED:
                return TurnOutcome.RETRY_EVIDENCE
            return _ignored(event, f"the worker lock was kept: {hold.blocker}", row.name)
        record = staff.snapshot()
        current = record.by_id(row.id)
        if (current is None or current.current_session != row.current_session
                or current.name != row.name or not hold.owns(current)):
            if event.kind == EventKind.RUN_FINISHED:
                return TurnOutcome.RETRY_EVIDENCE
            return _ignored(event, "the worker changed before the hook acquired its lock", row.name)
        row = current
        refreshed_parent = record.parent_of(row)
        if refreshed_parent is None or refreshed_parent.id != parent.id:
            return _ignored(event, "the worker owner changed before completion", row.name)
        parent = refreshed_parent
        if not isinstance(owner, WorkerByThread):
            event = replace(event, thread_id=str(row.current_session))
        run = worker_panes.worker_run(row, record=record)
        passed = worker_panes.worker_pass(row, run, record=record)
        worker.CompletionArtifacts(parent, run).validate_status(passed)

        instance = str(passed.get("instance_id", ""))
        delivery = progress_history.dispatch_delivery(passed)
        archived_stops = [(directory, receipt) for directory in _stopped_pass_folders(parent, record, run)
                          for receipt in progress_history.stopped_pass_receipts(directory, row.id)]

        def retry_stopped_diagnostics() -> None:
            for directory, receipt in archived_stops:
                try:
                    _send_stopped_diagnostics(row, parent, directory, receipt.instance_id)
                except (OSError, ValueError, staff.StaffUnreadable, staff.StaffRefused) as error:
                    unit_runs.report_anomaly(
                        " ".join(f"anomaly: {row.name}: close diagnostic retry failed: {error}".splitlines()))

        if archived_stops:
            _ = after_unlock.callback(retry_stopped_diagnostics)
        marker = _done_marker(event.reply) if event.kind == EventKind.TURN_END else ""
        late: TurnOutcome | NoLateDone = NoLateDone.UNRECORDED
        if event.kind == EventKind.RUN_FINISHED or (event.kind == EventKind.TURN_END
                and (not marker or marker != (delivery.completion_id or instance))):
            late = _recover_replaced_stops(event, row, parent, run, instance, marker, archived_stops, after_unlock)
            if marker and isinstance(late, TurnOutcome):
                return late

        def ignore_turn(reason: str, *, close_pane: bool = True) -> TurnOutcome:
            if late == TurnOutcome.STOPPED:
                return TurnOutcome.STOPPED
            if not close_pane:
                return _ignored(event, reason, row.name)
            # The Codex capacity stop cancels the pass, and a live detached run keeps the stopped pane at the
            # shell. The run end closes that pane. The canceled pass gates the pane scan.
            stopped = (event.kind == EventKind.RUN_FINISHED and passed.get("status") == "canceled" and isinstance(
                worker_panes.observe_worker(parent.id, row, record), worker_panes.StoppedWorker))
            if event.kind == EventKind.TURN_END or stopped:
                dispatch = worker_panes.worker_dispatch_state(row, run, hold)
                if not isinstance(dispatch, worker_panes.DispatchHoldsWorker):
                    closed = worker_panes.close(parent.id, staff.WorkerName.parse(row.name), lock=hold)
                    if stopped and not isinstance(closed, worker_panes.Closed):
                        blocker = closed.blocker if isinstance(closed, worker_panes.Kept) else closed.reason
                        reason = f"{reason}; the stopped pane did not close: {blocker}"
            return _ignored(event, reason, row.name)

        if event.kind in (EventKind.TURN_END, EventKind.RUN_FINISHED):
            worker_input = (detached_runs.WorkerResultInput.TURN_ENDED if event.kind == EventKind.TURN_END
                            else detached_runs.WorkerResultInput.TYPE)
            replay = detached_runs.submit_finished_worker_result(run, row, worker_input=worker_input)
            if replay == detached_runs.WorkerResultReplay.SUBMITTED:
                return TurnOutcome.SUBMITTED
            if replay == detached_runs.WorkerResultReplay.RETRY:
                return TurnOutcome.RETRY_EVIDENCE
        if not instance:
            return ignore_turn("no active dispatch or recoverable completion exists")
        stopped = progress_history.hook_completion(passed)
        if isinstance(stopped, progress_history.HookCompletion) and stopped.outcome == TurnOutcome.STOPPED:
            if passed.get("status") in ("canceled", "interrupted"):
                if stopped.finished:
                    return TurnOutcome.STOPPED
                receipt = progress_history.stopped_pass_receipt(run, passed)
                if event.kind == EventKind.RUN_FINISHED or (
                        event.kind == EventKind.TURN_END and marker in ("", receipt.completion_id)):
                    recovered = _record_stopped_reply(row, parent, run, receipt, stopped)
                    if isinstance(recovered, worker.DispatchEvidenceUnknown):
                        return (TurnOutcome.RETRY_EVIDENCE if event.kind == EventKind.RUN_FINISHED
                                else _ignored(event, recovered.reason, row.name))
                    if isinstance(recovered, StoppedReplyRecovery):
                        state = worker_panes.observe_worker(parent.id, row, record)
                        wait = _stopped_reply_wait(recovered, state, receipt, worker.CloseCompletedPane.CLOSE)
                        if wait == worker.CompletionRecovery.WAITING_ON_RUNS:
                            return TurnOutcome.WAITING_ON_RUNS
                        if wait == worker.CompletionRecovery.WAITING_ON_REPLY:
                            return TurnOutcome.WAITING_ON_REPLY
                    else:
                        stopped = recovered
                    decision = _stopped_notice(row, parent, run, passed, stopped)
                    return _close_stopped_after_notice(row, parent, run, instance, decision, event.kind)
                return _stopped_notice(row, parent, run, passed, stopped)
            return _ignored(event, "the recorder has not ended the stopped pass", row.name)
        client = herdr.Herdr(herdr.shared_session())
        state = worker_panes.observe_worker(parent.id, row, record)
        receipt = _recover_pass(event, row, parent, run, passed, delivery,
                                require_transcript=event.kind == EventKind.RUN_FINISHED)
        if isinstance(receipt, TurnOutcome):
            return receipt
        if isinstance(receipt, progress_history.HookCompletion):
            if receipt.finished:
                if isinstance(state, worker_panes.ClosedWorker):
                    _remove_completed_restart(row, delivery)
                return ignore_turn("the hook completion is already finished")
            return _finish_completion(row, parent, run, passed, instance, delivery, receipt, state)
        if isinstance(receipt, progress_history.NoHookCompletion):
            if passed.get("status") in ("active", "refused", "completed", "error"):
                return ignore_turn("the dispatch delivery line is empty")
            return ignore_turn("no active dispatch or recoverable completion exists")
        if isinstance(receipt, worker.DispatchEvidenceUnknown):
            reason = f"the transcript evidence is unknown: {receipt.reason}"
            _ = _ignored(event, reason, row.name)
            return TurnOutcome.RETRY_EVIDENCE if event.kind == EventKind.RUN_FINISHED else TurnOutcome.IGNORED
        if isinstance(state, worker_panes.WorkerNotObserved):
            return _ignored(event, "the worker pane was not observed", row.name)
        if isinstance(receipt, worker.DispatchNotSubmitted):
            if event.kind == EventKind.STAGE_READY and delivery.completion_id != instance:
                return _submit_stage(parent, row, state, client, run, passed, instance, delivery)
            return ignore_turn("the transcript has no matching dispatch prompt")
        turn_reply = receipt.reply if isinstance(receipt, worker.DispatchWaitingForRuns) else receipt
        reply = turn_reply.last_reply if isinstance(turn_reply, worker.DispatchAwaitingReply) else turn_reply.reply
        if (event.kind == EventKind.RUN_FINISHED and not isinstance(receipt, worker.DispatchComplete)
                and isinstance(state, worker_panes.OpenWorker) and state.status == herdr.AgentStatus.WORKING):
            return TurnOutcome.WAITING_ON_REPLY
        if isinstance(receipt, worker.DispatchSubmitted) and not receipt.reply:
            if event.kind == EventKind.STAGE_READY:
                return TurnOutcome.SUBMITTED
            if event.kind == EventKind.TURN_END and not event.reply and receipt.compacted_after_prompt:
                return _prepare_notice(row.name, parent, run, passed, instance, TurnOutcome.NO_DONE,
                                       Path(delivery.reply_file), "", "")
            return ignore_turn("the submitted dispatch has no final reply", close_pane=False)
        anomalies = receipt.anomalies if isinstance(receipt, worker.DispatchComplete) else ()
        if (event.kind == EventKind.TURN_END and (not reply or reply != event.reply)
                and not (isinstance(receipt, worker.DispatchComplete) and receipt.restored_done)):
            reason = "the transcript reply is empty" if not reply else "the transcript reply differs from the turn reply"
            return ignore_turn(reason, close_pane=False)
        stale_done = _stale_done_detail(reply, delivery.completion_id)
        if isinstance(receipt, worker.DispatchComplete):
            progress_history.confirm_assignment_submission(run, instance, delivery.completion_id)
            if Path(delivery.early_ready).exists():
                completion_id = worker_panes.stage_two_completion_id(instance)
                diff = delivery.early_ready.removesuffix(".ready") + ".diff"
                line = (f"Stage 2: the final diff is at {json.dumps(diff)}. "
                        + delivery.line.replace(f"DONE {instance}", f"DONE {completion_id}"))
                delivery = replace(delivery, line=line, completion_id=completion_id)
                progress_history.save_dispatch_delivery(run, instance, delivery)
                return _submit_stage(parent, row, state, client, run, passed, instance, delivery)
            outcome = TurnOutcome.WAITING_ON_STAGE_TWO
        elif isinstance(receipt, worker.DispatchWaitingForRuns):
            outcome = TurnOutcome.WAITING_ON_RUNS if receipt.has_live_run else TurnOutcome.WAITING_ON_REPLY
        elif isinstance(receipt, worker.DispatchAwaitingReply):
            outcome = TurnOutcome.WAITING_ON_REPLY
        elif not stale_done and _friend_pending(client, state, row):
            outcome = TurnOutcome.WAITING_ON_FRIEND
        else:
            outcome = TurnOutcome.NO_DONE
        path = Path(delivery.reply_file)
        path = path.with_name(path.stem + "_turn" + path.suffix)
        if reply:
            worker.CompletionArtifacts(parent, run).reply(path, reply)
        if outcome == TurnOutcome.NO_DONE and delivery.actions.end_worker and not stale_done:
            completion = progress_history.HookCompletion(outcome, str(path), anomalies)
            progress_history.save_hook_completion(run, instance, completion)
            return _finish_completion(row, parent, run, passed, instance, delivery, completion, state)
        details = stale_done if outcome == TurnOutcome.NO_DONE and stale_done else "\n".join(anomalies) or reply
        runs = receipt.runs if isinstance(receipt, worker.DispatchWaitingForRuns) else ()
        return _prepare_notice(row.name, parent, run, passed, instance, outcome, path, reply, details, runs)


def report_quota_refusals() -> list[str]:
    """Report a pane Codex worker's quota refusal once per pass; Codex runs no Stop hook for that turn."""
    from scripts.accounts import refusal_watch
    from scripts.whoami import agent_accounts, note_file, quota_alert

    record = staff.local_snapshot()
    lines: list[str] = []
    for row in record.rows:
        parent = record.parent_of(row)
        recorded = progress_history.recorded_dispatch_run(row.id)
        if row.kind != "worker" or parent is None or not isinstance(recorded, progress_history.RecordedDispatchRun):
            continue
        active = progress_history.active_dispatch(row.id, recorded.directory)
        backend = staff.backend(row)
        if (not isinstance(active, progress_history.ActiveDispatch) or not isinstance(backend, staff.CodexBackend)
                or worker_panes.worker_pass(row, recorded.directory).get("quota_refusal")):
            continue
        refusal = refusal_watch.transcript_refusal(str(row.current_session), config_dir=backend.codex_home)
        if not isinstance(refusal, refusal_watch.TranscriptRefusal):
            continue
        try:
            stopped = quota_alert.StoppedWorker(str(parent.current_session), row.name)
            lines += quota_alert.blocked(list(note_file.read_notes()), agent_accounts.codex_account(backend.codex_home),
                                         window=refusal.window, refused_at=refusal.refused_at, stopped=stopped)
            progress_history.record_quota_refusal(recorded.directory, active.instance_id, refusal.entry_id)
        except (OSError, ValueError) as error:
            lines.append(f"anomaly: {row.name}: quota refusal not reported: {' '.join(str(error).splitlines())}")
    return lines


def run_finished(owner: staff.StaffId) -> detached_runs.CompletionReplay:
    """Keep the run completion available when the transcript is unavailable."""
    outcome = handle(WorkerEvent("", EventKind.RUN_FINISHED), owner=owner)
    if outcome in (TurnOutcome.RETRY_EVIDENCE, TurnOutcome.SUBMITTED, TurnOutcome.WAITING_ON_REPLY):
        return detached_runs.CompletionReplay.RETRY
    return detached_runs.CompletionReplay.HANDLED


def main(arguments: list[str] | None = None) -> int:
    args = sys.argv[1:] if arguments is None else arguments
    try:
        if len(args) == 2 and args[0] == "stage-ready":
            record = staff.snapshot()
            parent = worker_admission.parent(record, staff_id=staff.runtime_id())
            row = worker_admission.find_worker(record, parent.id, staff.WorkerName.parse(args[1]).value)
            if row is None:
                raise ValueError(f"worker {args[1]} is not in this director's staff")
            _ = handle(WorkerEvent(str(row.current_session), EventKind.STAGE_READY))
            return 0
        separate = args == ["--handle-hook"]
        payload = args[0] if args and not separate else sys.stdin.read()
        value = cast(dict[str, object], json.loads(payload))
        if separate:
            _ = handle(hook_event(value))
        else:
            child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--handle-hook"],
                start_new_session=True, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            with cast(io.BufferedWriter, child.stdin) as stream:
                _ = stream.write(payload.encode())
        return 0
    except (OSError, ValueError, staff.StaffUnreadable, staff.StaffRefused, herdr_types.HerdrFailure) as error:
        print(f"turn_end: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

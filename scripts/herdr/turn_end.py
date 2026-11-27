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
import shlex
import subprocess
import sys
import time
import uuid
from collections.abc import Generator, Iterable
from contextlib import AbstractContextManager, ExitStack, redirect_stderr
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from enum import Enum, StrEnum
from pathlib import Path
from typing import Callable, Literal, TypeGuard, cast

from scripts.agents import detached_runs, remote, shutdown_record, worker_lifecycle
from scripts.direct import pass_record as pass_recorder
from scripts.direct import progress_history, recorder_state
from scripts.herdr import herdr_types
from scripts.lib import atomic_file, conf, scratch, unit_runs
from scripts.herdr import herdr, pass_notices, worker_panes
from scripts.herdr.worker_panes import StoppedDiagnosticRecovery
from scripts.message import send
from scripts.production import staff, worker, worker_admission


class EventKind(StrEnum):
    TURN_END = "turn end"
    RUN_FINISHED = "run finished"
    STAGE_READY = "stage ready"


class NoNativeStop(Enum):
    ABSENT = "the event has no native Stop payload"


@dataclass(frozen=True)
class WorkerEvent:
    thread_id: str
    kind: EventKind = EventKind.TURN_END
    reply: str = ""
    cwd: str = ""
    native_stop: dict[str, object] | NoNativeStop = NoNativeStop.ABSENT
    source_event_id: str = ""


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
    WAITING_ON_REAP = "waiting for reap to record the run result"
    WAITING_ON_STAGE_TWO = "waiting on final diff"
    RETRY_EVIDENCE = "completion evidence needs another read"
    SUBMITTED = "submitted"
    IGNORED = "ignored"


def hook_event(value: dict[str, object]) -> WorkerEvent:
    """Convert each native hook input at the boundary."""
    if value.get("hook_event_name") == "Stop":
        thread = str(value.get("session_id", ""))
        turn = pass_recorder.string(value.get("turn_id"))
        return WorkerEvent(thread, reply=str(value.get("last_assistant_message") or ""),
                           cwd=str(value.get("cwd") or ""), native_stop=value,
                           source_event_id=f"{thread}:{turn}" if turn else "")
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


def _status_transition(outcome: TurnOutcome, reply: str, details: str, runs: tuple[str, ...]) -> str:
    return hashlib.sha256(json.dumps((outcome, reply, details, runs)).encode()).hexdigest()


def _prepare_notice(worker_name: str, parent: staff.StaffRow, run: Path, passed: dict[str, object],
                    instance: str, outcome: TurnOutcome, path: Path, reply: str, details: str,
                    runs: tuple[str, ...] = (), *, notice_type: str = "", defer_notice: bool = False,
                    source_event_id: str = "", saved_reply_path: bool = False,
                    turn_cursor: worker.TranscriptCursor | worker.NoTranscriptTurn = worker.NoTranscriptTurn.UNRECORDED,
                    ) -> TurnDecision:
    """Save the result before it can leave the worker pass."""
    busy = outcome == TurnOutcome.BUSY
    phase = pass_recorder.object_dict(passed.get("phase")) or {}
    result_text = "the dispatch attempt found the worker busy" if busy else outcome.value
    message = f"{worker_name}: phase {phase.get('id', '')}: {result_text}"
    if reply or saved_reply_path:
        message += f"; reply {path}"
    message += f"\nPass {instance}." if outcome == TurnOutcome.STOPPED else f"; pass {instance}"
    if runs:
        message += "; runs " + ", ".join(runs)
    identity = staff.StaffId(str(passed.get("worker_id", "")))
    completion_id = pass_recorder.dispatch_delivery(passed).completion_id
    if busy:
        launcher = passed.get("launcher_command")
        if isinstance(launcher, list) and launcher and all(isinstance(arg, str) for arg in cast(list[object], launcher)):
            message += "\nSaved launcher: " + shlex.join(cast(list[str], launcher))
        message += "\nRetry this delivery: " + shlex.join(
            worker.finish_dispatch_command(parent.id, identity, run, instance, completion_id))
    if details:
        message += "\n" + details
    completion = progress_history.hook_completion(passed)
    if isinstance(completion, progress_history.HookCompletion) and completion.sender_native_id:
        sender = completion.sender_native_id
    else:
        member = staff.snapshot().by_id(identity)
        sender = _native_sender(member) if member is not None else f"codex:{identity}"
    route = pass_notices.Route(instance, parent.id, worker_name)
    saved_stamp = pass_recorder.string(passed.get("hook_notice"))
    completion_stamp = f"completion:{outcome.value}"
    incoming_stamp = (completion_stamp if notice_type and saved_stamp == completion_stamp
                      else hashlib.sha256("\n".join((outcome.value, reply, *runs)).encode()).hexdigest())
    legacy = pass_notices.LegacyNoticeStamp(saved_stamp, incoming_stamp)
    if notice_type:
        notice = pass_notices.Notice(message, sender, pass_notices.NoticeKind.RESULT)
        # A stopped notice saved before the late reply gives way to the text with the reply path.
        staged = pass_notices.StagedText.REPLACES_UNSETTLED if saved_reply_path else pass_notices.StagedText.FIRST
        pass_notices.stage_result(run, route, notice_type, notice, legacy=legacy, staged=staged)
        if defer_notice:
            return TurnDecision(outcome, _no_notice)
        return TurnDecision(outcome, lambda: pass_notices.deliver(run, instance, (notice_type,)))

    unconfirmed = busy or outcome == TurnOutcome.UNCERTAIN
    notice = pass_notices.Notice(message, sender, pass_notices.NoticeKind.STATUS,
                                 transition=_status_transition(outcome, reply, details, runs),
                                 source_event_id=f"busy:{uuid.uuid4()}" if busy else source_event_id,
                                 unconfirmed_delivery=completion_id if unconfirmed else "")
    if unconfirmed:
        def send_while_unconfirmed() -> None:
            # Save the notice only while the recorder holds the exact delivery unconfirmed.
            pending = (progress_history.busy_dispatch_notice_pending(run, identity, instance, completion_id,
                                                                     owner_id=parent.id)
                       if busy else progress_history.dispatch_delivery_pending(run, instance, completion_id))
            if pending:
                event = pass_notices.stage_status(run, route, notice, legacy=legacy)
                pass_notices.deliver_status(run, instance, event)

        return TurnDecision(outcome, send_while_unconfirmed)
    if isinstance(turn_cursor, worker.TranscriptCursor):
        cursor = turn_cursor

        def send_latest_turn() -> None:
            # Save a turn notice only while it is still the latest turn of the current pass.
            with worker_panes.worker_id_lock(identity, blocking=True):
                if _latest_turn_notice(run, parent, identity, instance, completion_id, cursor):
                    turn = replace(notice, source_event_id=pass_notices.TURN_EVENT + cursor.prefix)
                    event = pass_notices.stage_status(run, route, turn, legacy=legacy)
                    pass_notices.deliver_status(run, instance, event)
                    path = worker.pending_done_path(run, identity)
                    pending = worker_lifecycle.read_json_object(path)
                    pending["delivered_notice"] = asdict(cursor)
                    atomic_file.write_text_atomic(path, json.dumps(pending))

        return TurnDecision(outcome, send_latest_turn)
    event = pass_notices.stage_status(run, route, notice, legacy=legacy)
    return TurnDecision(outcome, lambda: pass_notices.deliver_status(run, instance, event))


def _latest_turn_notice(run: Path, parent: staff.StaffRow, identity: staff.StaffId, instance: str,
                        completion_id: str, cursor: worker.TranscriptCursor) -> bool:
    record = staff.snapshot()
    member = record.by_id(identity)
    if member is None or not member.reports_to(parent.id) or worker_panes.worker_run(member, record=record) != run:
        return False
    current = worker_panes.worker_pass(member, run, record=record)
    if (current.get("instance_id") != instance or current.get("status") not in ("active", "refused")
            or pass_recorder.dispatch_delivery(current).completion_id != completion_id
            or isinstance(progress_history.hook_completion(current), progress_history.HookCompletion)):
        return False
    pending = worker_lifecycle.read_json_object(worker.pending_done_path(run, identity))
    delivered = worker.TranscriptCursor.from_json(pending.get("delivered_notice"))
    if (pending.get("instance") != instance or pending.get("stage") != (completion_id or instance)
            or pending.get("latest_notice") != asdict(cursor)
            or isinstance(delivered, worker.TranscriptCursor) and (
                delivered.stream != cursor.stream or delivered.at_or_after(cursor))):
        return False
    backend = staff.backend(member)
    if isinstance(backend, staff.BackendUnavailable):
        return False
    try:
        return worker.transcript_stream(member, worker_admission.transcript_source(member, backend)) == cursor.stream
    except (OSError, ValueError):
        return False


def _native_sender(row: staff.StaffRow) -> str:
    prefix = "session:" if isinstance(staff.backend(row), staff.ClaudeBackend) else "codex:"
    return prefix + str(row.current_session)


def _completion_reply(completion: progress_history.HookCompletion) -> str:
    if completion.outcome == TurnOutcome.STOPPED:
        return ""
    try:
        return Path(completion.reply_file).read_text()
    except FileNotFoundError:
        if completion.outcome != TurnOutcome.ERROR:
            raise
        return "\n".join(completion.details)


def _completion_notice_details(completion: progress_history.HookCompletion) -> str:
    # Old scratch notices have no saved path to check.
    return "\n".join(detail for detail in completion.details if not scratch.is_retained_task_folder_notice(detail))


def _stopped_notice(row: staff.StaffRow, parent: staff.StaffRow, run: Path, passed: dict[str, object],
                    completion: progress_history.HookCompletion, *, defer_notice: bool = False) -> TurnDecision:
    return _prepare_notice(row.name, parent, run, {**passed, "hook_completion": asdict(completion)},
                           str(passed["instance_id"]), TurnOutcome.STOPPED, Path(completion.reply_file),
                           "", "\n".join(completion.details), notice_type=TurnOutcome.STOPPED,
                           defer_notice=defer_notice, saved_reply_path=bool(completion.reply_file)
                           and completion.stopped_reply is progress_history.StoppedReplyDisposition.SAVED)


def _detached_stop_completion(decision: TurnDecision) -> worker_panes.StoppedRunPassCompletion:
    def complete() -> worker_panes.StoppedPassCleanup:
        if decision.send_notice() == TurnOutcome.RETRY_EVIDENCE:
            return worker_panes.StoppedPassCleanup.RETRY
        return worker_panes.StoppedPassCleanup.COMPLETE

    return worker_panes.StoppedRunPassCompletion(decision.outcome, complete)


def _recorded_stop_notice(run: Path, notice: progress_history.StoppedPassNotice) -> worker_panes.StoppedRunPassCompletion:
    def retry_diagnostics() -> TurnOutcome | None:
        if retry_stopped_diagnostics(run, notice.worker_id) is not StoppedDiagnosticRecovery.SETTLED:
            return TurnOutcome.RETRY_EVIDENCE
        return None

    passed = progress_history.pass_for_finalization(run, notice.instance_id)
    delivery = progress_history.dispatch_delivery(passed)
    settled = pass_notices.is_settled(run, notice.instance_id, TurnOutcome.STOPPED)
    if settled and not delivery.actions.finish_run and passed.get("status") != "canceled":
        return _detached_stop_completion(TurnDecision(TurnOutcome.STOPPED, retry_diagnostics))
    record = staff.snapshot()
    director = record.by_id(notice.director_id)
    if director is None:
        if settled:
            return _detached_stop_completion(TurnDecision(TurnOutcome.STOPPED, retry_diagnostics))
        raise ValueError(f"{notice.director_id}: stopped pass notice recipient is not in staff")
    row = record.by_id(notice.worker_id)
    completion = progress_history.hook_completion(passed)
    # A saved late reply puts its path in the notice text.
    if (row is not None and isinstance(completion, progress_history.HookCompletion)
            and completion.stopped_reply == progress_history.StoppedReplyDisposition.SAVED):
        decision = _stopped_notice(row, director, run, passed, completion)
    else:
        recipient_fields: dict[str, object] = {"worker_id": notice.worker_id, "phase": {"id": notice.phase_id}}
        decision = _prepare_notice(notice.worker_name, director, run, recipient_fields, notice.instance_id,
                                   TurnOutcome.STOPPED, Path(), "", notice.reason, notice_type=TurnOutcome.STOPPED)
    if (row is not None and row.reports_to(director.id)
            and isinstance(completion, progress_history.HookCompletion) and completion.outcome == TurnOutcome.STOPPED
            and (delivery.actions.finish_run or passed.get("status") == "canceled")):
        decision = _close_stopped_after_notice(row, director, run, notice.instance_id, completion, decision,
                                               EventKind.RUN_FINISHED)
        return _detached_stop_completion(decision)

    def send_notice() -> TurnOutcome | None:
        _ = decision.send_notice()
        return retry_diagnostics()

    return _detached_stop_completion(replace(decision, send_notice=send_notice))


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
        delivery = pass_recorder.dispatch_delivery(passed)
        if isinstance(completion, progress_history.HookCompletion) and completion.outcome == TurnOutcome.STOPPED:
            if passed.get("status") == "canceled" or (passed.get("status") == "interrupted"
                    and pass_notices.is_settled(run, str(instance), TurnOutcome.STOPPED)):
                return _detached_stop_completion(_stopped_notice(current, parent, run, passed, completion))
        if (passed.get("status") == "completed" and isinstance(instance, str)
                and isinstance(completion, progress_history.HookCompletion) and completion.outcome == TurnOutcome.DONE):
            if completion.finished:
                return _detached_stop_completion(TurnDecision(TurnOutcome.DONE, _no_notice))
            event = WorkerEvent(str(current.current_session), EventKind.RUN_FINISHED)
            recovered = _recover_pass(event, current, parent, run, passed, delivery, require_transcript=True)
            if not isinstance(recovered, progress_history.HookCompletion):
                return worker_panes.Kept("the pass awaits verified completion")
            state = worker_panes.observe_worker(parent.id, current, record)
            decision = _finish_completion(current, parent, run, passed, instance, delivery, recovered, state)
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
            wait_for_results = isinstance(receipt, worker.DispatchWaitingForRuns) and not (
                isinstance(receipt.reply, worker.DispatchSubmitted) and receipt.runs == (stopped["run_id"],))
            if (isinstance(receipt, (worker.DispatchComplete, worker.DispatchAwaitingReply)) or wait_for_results
                    or receipt in (TurnOutcome.WAITING_ON_RUNS, TurnOutcome.WAITING_ON_REPLY,
                                   TurnOutcome.WAITING_ON_REAP)):
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


def retry_completed_notice(worker_name: str, parent: staff.StaffRow, run: Path,
                           passed: dict[str, object]) -> Callable[[], None]:
    """Retry the saved result after the worker lock is released."""
    worker.CompletionArtifacts(parent, run).validate_status(passed)
    instance = str(passed["instance_id"])
    completion = progress_history.hook_completion(passed)
    delivery = pass_recorder.dispatch_delivery(passed)
    outcome = TurnOutcome(completion.outcome) if isinstance(completion, progress_history.HookCompletion) else TurnOutcome.DONE
    path = Path(completion.reply_file if isinstance(completion, progress_history.HookCompletion) else delivery.reply_file)
    details = _completion_notice_details(completion) if isinstance(completion, progress_history.HookCompletion) else ""
    reply = _completion_reply(completion) if isinstance(completion, progress_history.HookCompletion) else path.read_text()
    if delivery.actions.status_file:
        worker.CompletionArtifacts(parent, run).write(Path(delivery.actions.status_file), "done\n")

    def resend() -> None:
        key = _result_notice_type(outcome, reply, delivery)
        _ = _prepare_notice(worker_name, parent, run, passed, instance, outcome, path, reply, details,
                            notice_type=key, defer_notice=True)
        pass_notices.retry_result(run, instance, key)

    return resend


def _submit_stage(parent: staff.StaffRow, row: staff.StaffRow, state: worker_panes.WorkerPane,
                  client: herdr.Herdr, run: Path, passed: dict[str, object], instance: str,
                  delivery: pass_recorder.DispatchDelivery, hold: worker_panes.WorkerLockHold) -> TurnOutcome | TurnDecision:
    record = staff.snapshot()
    availability = worker.CheckAssignmentAvailability.for_worker(record, row, run)

    def before_input(target: staff.StaffRow) -> worker_panes.CloseAllowed | worker_panes.DispatchHoldsWorker:
        return availability.check(target, hold, retry=worker.RetryingAssignment(run.resolve(), instance, delivery))

    with redirect_stderr(io.StringIO()) as diagnostics:
        try:
            result = (worker.dispatch_turn(parent.id, state, client, delivery.line, delivery.completion_id, run=run,
                                           instance_id=instance, before_input=before_input)
                      if isinstance(state, worker_panes.OpenWorker) else worker.ExitCode.UNCERTAIN)
        except (OSError, herdr_types.HerdrFailure, subprocess.SubprocessError) as error:
            print(str(error), file=sys.stderr)
            result = worker.ExitCode.UNCERTAIN
    if result is worker.DispatchSubmission.SUBMITTED:
        progress_history.confirm_assignment_submission(run, instance, delivery.completion_id,
                                                        worker_thread=str(row.current_session))
        return TurnOutcome.SUBMITTED
    outcome = TurnOutcome.BUSY if result == worker.ExitCode.BUSY else TurnOutcome.UNCERTAIN
    passed = {**passed, "delivery": asdict(delivery)}
    details = diagnostics.getvalue().strip() or f"Stage 2 submission returned {result}"
    path = Path(delivery.reply_file)
    path = path.with_name(path.stem + "_turn" + path.suffix)
    worker.CompletionArtifacts(parent, run).reply(path, details)
    return _prepare_notice(row.name, parent, run, passed, instance, outcome, path, details, details)


def _result_notice_type(outcome: TurnOutcome, reply: str, delivery: pass_recorder.DispatchDelivery) -> str:
    if outcome == TurnOutcome.ERROR and worker_admission.reply_ends_dispatch(reply, delivery.completion_id):
        return TurnOutcome.DONE
    return outcome


def _finish_completion(row: staff.StaffRow, parent: staff.StaffRow, run: Path, passed: dict[str, object],
                       instance: str, delivery: pass_recorder.DispatchDelivery,
                       completion: progress_history.HookCompletion,
                       state: worker_panes.WorkerPane | worker_panes.WorkerNotObserved, *,
                       pane: worker.CloseCompletedPane | worker.ReuseCompletedPane | worker.KeepCompletedPane = worker.CloseCompletedPane.CLOSE,
                       lock: worker_panes.WorkerLockHold | worker_panes.WorkerLockAtUse = worker_panes.WorkerLockAtUse.ACQUIRE,
                       after_pass_recorded: Callable[[], None] = _no_notice,
                       ) -> TurnDecision:
    from scripts.direct import assign

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
    try:
        pass_notices.save_completion_scratch(run, instance, scratch.end(scratch.worker_task(row.id)))
    except Exception as error:
        diagnostic = f"scratch cleanup failed for {row.name}: {' '.join(str(error).splitlines())}"
        if diagnostic not in completion.details:
            completion = replace(completion, details=(*completion.details, diagnostic))
            progress_history.save_hook_completion(run, instance, completion)
    reply = _completion_reply(completion)

    decision = _prepare_notice(row.name, parent, run, {**passed, "hook_completion": asdict(completion)},
                               instance, outcome, path, reply, _completion_notice_details(completion),
                               notice_type=_result_notice_type(outcome, reply, delivery),
                               defer_notice=not isinstance(pane, worker.CloseCompletedPane))
    if outcome != TurnOutcome.LATE_DONE and passed.get("status") in ("active", "refused"):
        progress_history.complete_dispatch(run, str(passed.get("team_slot", row.name)), instance,
                                           status="completed" if outcome == TurnOutcome.DONE else "error")
    after_pass_recorded()
    if isinstance(pane, worker.KeepCompletedPane):
        return decision

    def finalize(hold: worker_panes.WorkerLockHold) -> TurnOutcome:
        record = staff.snapshot()
        member = record.by_id(row.id)
        if (member is None or member.name != row.name or member.current_session != row.current_session
                or not hold.owns(member) or worker_panes.worker_run(member, record=record) != run):
            return outcome
        current = worker_panes.worker_pass(member, run, record=record)
        saved = progress_history.hook_completion(current)
        if (current.get("instance_id") != instance or not isinstance(saved, progress_history.HookCompletion)
                or saved.finished or not _same_completion(saved, completion)):
            return outcome
        current_state = state
        if (isinstance(lock, worker_panes.WorkerLockAtUse) and isinstance(pane, worker.CloseCompletedPane)
                and saved.pane_closed and isinstance(state, worker_panes.ClosedWorker)):
            current_state = worker_panes.observe_worker(parent.id, member, record)
        return _finalize_completion(member, parent, run, instance, delivery, saved, current_state, hold, pane)

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
                         delivery: pass_recorder.DispatchDelivery, completion: progress_history.HookCompletion,
                         state: worker_panes.WorkerPane | worker_panes.WorkerNotObserved,
                         hold: worker_panes.WorkerLockHold, pane: worker.CloseCompletedPane | worker.ReuseCompletedPane,
                         ) -> TurnOutcome:
    from scripts.direct import assign

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
        if isinstance(pane, worker.CloseCompletedPane) and not (
                completion.pane_closed and isinstance(state, worker_panes.ClosedWorker)):
            # Reuse can save the flag while the pane is still open.
            completion = replace(completion, pane_closed=False)
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
    recording_done = completion.recording_done
    if not recording_done and (closed or not needs_recording_after_close):
        status = "stopped" if stopped else "completed" if outcome == TurnOutcome.DONE and completion.pane_closed else "error"
        result = assign.finish_herdr_recording(run, instance, delivery.actions, status=status)
        if isinstance(result, assign.CompletionFailed):
            diagnostics.append(result.reason if outcome == TurnOutcome.DONE else f"{outcome}: {result.reason}")
            if not stopped:
                outcome = TurnOutcome.ERROR
        recording_done = not needs_recording_after_close or isinstance(result, assign.CompletionApplied)
    pending = tuple(dict.fromkeys((*completion.pending_diagnostics, *diagnostics)))
    if pending != completion.pending_diagnostics:
        completion = replace(completion, pending_diagnostics=pending)
        progress_history.save_hook_completion(run, instance, completion)
    route = pass_notices.Route(instance, parent.id, row.name)
    sender = completion.sender_native_id or _native_sender(row)
    if isinstance(pane, worker.CloseCompletedPane):
        pass_notices.import_legacy_diagnostics(run, route, sender)
    _ = pass_notices.stage_diagnostics(run, route, sender, pending)
    details = tuple(dict.fromkeys((*completion.details, *pending)))
    completion = replace(completion, details=details, recording_done=recording_done, pending_diagnostics=())
    progress_history.save_hook_completion(run, instance, completion)
    ready_to_finish = closed and completion.recording_done
    has_done = completion.outcome in (TurnOutcome.DONE, TurnOutcome.LATE_DONE)
    if ready_to_finish and has_done:
        detached_runs.stage_worker_stop_results(run, row, instance)
    if stopped:
        _defer_stopped_diagnostics(hold, run, row.id)
    elif isinstance(pane, worker.CloseCompletedPane):
        _ = pass_notices.deliver_diagnostics(run, instance)
    if ready_to_finish:
        progress_history.save_hook_completion(run, instance, replace(completion, finished=True))
        pending = worker.pending_done_path(run, row.id)
        if worker_lifecycle.read_json_object(pending).get("instance") == instance:
            pending.unlink(missing_ok=True)
        if has_done:
            passed = worker_panes.worker_pass(row, run)
            if (passed.get("instance_id") == instance
                    and detached_runs.acknowledge_worker_completion(run, row, passed) == detached_runs.CompletionReplay.RETRY):
                return TurnOutcome.RETRY_EVIDENCE
        if isinstance(pane, worker.CloseCompletedPane):
            _remove_completed_restart(row, delivery)
    return outcome


def _remove_completed_restart(row: staff.StaffRow, delivery: pass_recorder.DispatchDelivery) -> None:
    shutdown_record.forget([str(row.id)])
    if delivery.actions.end_worker:
        _ = staff.retire(row.id)


def recover_completion(row: staff.StaffRow, parent: staff.StaffRow, run: Path,
                       hold: worker_panes.WorkerLockHold, *, record: staff.StaffSnapshot,
                       pane: worker.CloseCompletedPane | worker.ReuseCompletedPane | worker.KeepCompletedPane = worker.CloseCompletedPane.CLOSE,
                       ) -> worker.CompletionRecovery:
    """Recover the pass before restore closes the pane or dispatch reuses it."""
    _defer_stopped_diagnostics(hold, run, row.id)
    passed = worker_panes.worker_pass(row, run, record=record)
    instance = str(passed.get("instance_id", ""))
    if not instance:
        return worker.CompletionRecovery.NO_PENDING_NOTICE
    stopped = progress_history.hook_completion(passed)
    if isinstance(stopped, progress_history.HookCompletion) and stopped.outcome == TurnOutcome.STOPPED:
        if passed.get("status") == "canceled":
            receipt = progress_history.stopped_pass_receipt(run, passed)
            recovered = _record_stopped_reply(row, parent, run, receipt, stopped)
            if isinstance(recovered, worker.DispatchEvidenceUnknown):
                raise ValueError(recovered.reason)
            if recovered == StoppedReplyRecovery.WAITING_ON_RUNS:
                return worker.CompletionRecovery.WAITING_ON_RUNS
            if recovered is StoppedReplyRecovery.WAITING_ON_REPLY:
                if not isinstance(pane, worker.ReuseCompletedPane):
                    return worker.CompletionRecovery.WAITING_ON_REPLY
                state = worker_panes.observe_worker(parent.id, row, record)
                wait = _stopped_reply_wait(recovered, state, receipt, pane)
                if isinstance(wait, worker.CompletionRecovery):
                    return wait
            if recovered == StoppedReplyRecovery.WAITING_ON_REAP:
                return worker.CompletionRecovery.WAITING_ON_REAP
            completion = recovered if isinstance(recovered, progress_history.HookCompletion) else stopped
            close = isinstance(pane, worker.CloseCompletedPane)
            _ = _stopped_notice(row, parent, run, passed, completion, defer_notice=not close).send_notice()
            if close and isinstance(recovered, progress_history.HookCompletion):
                state = worker_panes.observe_worker(parent.id, row, record)
                return _finalize_stopped(row, parent, run, instance, pass_recorder.dispatch_delivery(passed),
                                         receipt, completion, state, hold, pane)
        return worker.CompletionRecovery.NO_PENDING_NOTICE
    delivery = pass_recorder.dispatch_delivery(passed)
    event = WorkerEvent(str(row.current_session), EventKind.RUN_FINISHED)
    completion = _recover_pass(event, row, parent, run, passed, delivery, require_transcript=True)
    if isinstance(completion, worker.DispatchEvidenceUnknown):
        raise ValueError(completion.reason)
    if completion == TurnOutcome.WAITING_ON_RUNS:
        return worker.CompletionRecovery.WAITING_ON_RUNS
    if completion == TurnOutcome.WAITING_ON_REAP:
        return worker.CompletionRecovery.WAITING_ON_REAP
    if completion == TurnOutcome.WAITING_ON_REPLY:
        return worker.CompletionRecovery.WAITING_ON_REPLY
    if isinstance(completion, (worker.DispatchWaitingForRuns, worker.DispatchAwaitingReply)):
        waiting = worker.waiting_recovery(completion)
        before_done = (isinstance(completion, worker.DispatchWaitingForRuns)
                       and isinstance(completion.reply, worker.DispatchSubmitted))
        # A live run before the first DONE leaves no completion to recover.
        return (worker.CompletionRecovery.NO_PENDING_NOTICE
                if waiting is worker.CompletionRecovery.WAITING_ON_RUNS and before_done else waiting)
    if completion == TurnOutcome.IGNORED:
        raise ValueError(f"{row.name}: the saved DONE has no accepted dispatch receipt")
    if not isinstance(completion, progress_history.HookCompletion):
        return worker.CompletionRecovery.NO_PENDING_NOTICE
    if isinstance(pane, worker.KeepCompletedPane):
        if passed.get("status") in ("active", "refused"):
            _ = _finish_completion(row, parent, run, passed, instance, delivery, completion,
                                   worker_panes.WorkerNotObserved.CURRENT, pane=pane, lock=hold)
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


def _report_missing_worker(event: WorkerEvent, record: staff.StaffSnapshot) -> None:
    if event.kind != EventKind.TURN_END or not event.thread_id or not event.reply.strip():
        return
    from scripts.message import send

    directory = Path(event.cwd).resolve() if event.cwd else Path.cwd()
    supplied_run = unit_runs.environ("RUN_DIR")
    if supplied_run:
        run = Path(supplied_run)
    else:
        directory_owner = record.by_worktree(remote.local_machine(), directory)
        if directory_owner is None or directory_owner.kind not in ("unit", "showrunner"):
            return
        run = worker_panes.worker_run(directory_owner, record=record)
    state = worker_panes.run_state(run)
    if not progress_history.submitted_worker_thread(state, event.thread_id):
        return
    owner = unit_runs.run_owner(state, record)
    if not isinstance(owner, staff.StaffRow):
        return
    while owner is not None and owner.kind != "showrunner":
        owner = record.parent_of(owner)
    if owner is None:
        _log_hook_event(event, f"no showrunner owns the worker directory {directory}", "unknown")
        return
    tail = "\n".join(event.reply.rstrip().splitlines()[-8:])[-1600:]
    digest = hashlib.sha256(f"{event.thread_id}\n{event.reply}".encode()).hexdigest()
    message = f"Worker turn has no staff row.\nThread {event.thread_id}\nDirectory {directory}\n\n{tail}"
    result = send.send(send.parse(["--to", str(owner.id), "--from", "worker-turn-end", "--text", message,
                                   "--key", f"worker-turn-no-staff:{digest}", "--recipient-key", f"staff:{owner.id}",
                                   "--repeat-minutes", str(pass_notices.REPEAT_MINUTES)]))
    if result.outcome not in ("sent", "queued", "skipped"):
        _log_hook_event(event, f"missing worker notice failed: {result.detail}", "unknown")


class WorkerByThread(Enum):
    LOOKUP = "resolve the worker from the hook thread"


def handle(event: WorkerEvent, *, owner: staff.StaffId | WorkerByThread = WorkerByThread.LOOKUP) -> TurnOutcome:
    """Send the decided notice after releasing the worker lock."""
    return _handle_event(event, owner)


def _handle_event(event: WorkerEvent, owner: staff.StaffId | WorkerByThread, *,
                  result_pass: str = "", retry_run: Path | worker_panes.HolderRun = worker_panes.HolderRun.CURRENT,
                  ) -> TurnOutcome:
    try:
        decision = _handle_locked(event, owner, result_pass=result_pass, retry_run=retry_run)
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
    WAITING_ON_REAP = worker.CompletionRecovery.WAITING_ON_REAP.value


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
    reply = worker.dispatch_receipt(row, receipt.line, receipt.completion_id, run=run,
                                    require_transcript=True, pane_pass=receipt,
                                    verify=worker.VerifyPassDone(receipt.instance_id))
    if isinstance(reply, worker.DispatchEvidenceUnknown):
        return reply
    if isinstance(reply, worker.DispatchWaitingForRuns):
        if reply.waits_only_for_reap:
            return StoppedReplyRecovery.WAITING_ON_REAP
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
    if recovery == StoppedReplyRecovery.WAITING_ON_REAP:
        return worker.CompletionRecovery.WAITING_ON_REAP
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
    # A pending notice blocks only a close. A dispatch that reuses the pane continues.
    if isinstance(pane, worker.CloseCompletedPane) and not pass_notices.is_settled(run, instance, TurnOutcome.STOPPED):
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
            _ = _record_stopped_close_diagnostic(row, parent, run, instance, str(error))
            if report is None:
                raise
            report(str(error))
            return worker.CompletionRecovery.PENDING_CLOSE
        if not isinstance(closed, worker_panes.Closed):
            reason = closed.blocker if isinstance(closed, worker_panes.Kept) else closed.reason
            _ = _record_stopped_close_diagnostic(row, parent, run, instance, reason)
            if report is None:
                raise ValueError(reason)
            report(reason)
            return worker.CompletionRecovery.PENDING_CLOSE
        completion = replace(completion, pane_closed=True)
        progress_history.save_hook_completion(run, instance, completion)
        state = worker_panes.ClosedWorker(row)
    _ = _finalize_completion(row, parent, run, instance, delivery, completion, state, hold, pane)
    saved = progress_history.hook_completion(progress_history.pass_for_finalization(run, instance))
    if isinstance(saved, progress_history.HookCompletion) and saved.finished:
        return worker.CompletionRecovery.RECOVERED
    return worker.CompletionRecovery.PENDING_CLOSE


def _record_stopped_close_diagnostic(row: staff.StaffRow, parent: staff.StaffRow, run: Path, instance: str,
                                     reason: str) -> str:
    message = f"{row.name}: kept pane: {' '.join(reason.splitlines())}"
    _ = pass_notices.stage_diagnostics(run, pass_notices.Route(instance, parent.id, row.name), _native_sender(row),
                                       (message,))
    return message


def _close_stopped_after_notice(row: staff.StaffRow, parent: staff.StaffRow, run: Path, instance: str,
                                expected: progress_history.HookCompletion, decision: TurnDecision,
                                event_kind: EventKind) -> TurnDecision:
    def report(reason: str) -> None:
        message = _record_stopped_close_diagnostic(row, parent, run, instance, reason)
        print(message, file=sys.stderr)
        _ = retry_stopped_diagnostics(run, row.id, propagate_error_for=instance)

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
                _defer_stopped_diagnostics(hold, run, row.id)
                return TurnOutcome.RETRY_EVIDENCE if event_kind == EventKind.RUN_FINISHED else None
            passed = progress_history.pass_for_finalization(run, instance)
            saved = progress_history.hook_completion(passed)
            if (passed.get("worker_id") != current.id or passed.get("status") != "canceled"
                    or not isinstance(saved, progress_history.HookCompletion) or saved.outcome != TurnOutcome.STOPPED):
                _defer_stopped_diagnostics(hold, run, row.id)
                return None
            state = worker_panes.observe_worker(parent.id, current, record)
            receipt = progress_history.stopped_pass_receipt(run, passed)
            delivery = progress_history.dispatch_delivery(passed)
            use = _stopped_pane_use(current, run, receipt, passed, record)
            if isinstance(use, worker.DispatchEvidenceUnknown):
                _ = after_unlock.callback(report, use.reason)
                return TurnOutcome.RETRY_EVIDENCE if event_kind == EventKind.RUN_FINISHED else None
            pane, ownership = use
            finalize = (delivery.actions.finish_run or ownership is StoppedPassPane.REPLACED
                        or saved.stopped_reply is progress_history.StoppedReplyDisposition.SAVED)
            if not finalize:
                if (current.current_session != row.current_session or saved.pane_closed
                        or not _same_completion(saved, expected)
                        or not pass_notices.is_settled(run, instance, TurnOutcome.STOPPED)):
                    return None
                recovered = _record_stopped_reply(current, parent, run, receipt, saved)
                try:
                    with redirect_stderr(io.StringIO()):
                        closed = worker_panes.close(parent.id, name, lock=hold)
                except (OSError, ValueError, herdr_types.HerdrFailure, subprocess.SubprocessError,
                        staff.StaffRefused, staff.StaffUnreadable) as error:
                    _ = after_unlock.callback(report, str(error))
                    return None
                if isinstance(closed, worker_panes.Closed):
                    latest = recovered if isinstance(recovered, progress_history.HookCompletion) else saved
                    progress_history.save_hook_completion(run, instance, replace(latest, pane_closed=True))
                elif isinstance(closed, worker_panes.UncertainWorker) or isinstance(recovered, progress_history.HookCompletion):
                    _ = after_unlock.callback(report, closed.blocker if isinstance(closed, worker_panes.Kept) else closed.reason)
                _defer_stopped_diagnostics(hold, run, row.id)
                return None
            reported = False

            def report_after_unlock(reason: str) -> None:
                nonlocal reported
                reported = True
                _ = after_unlock.callback(report, reason)

            try:
                recovery = _finalize_stopped(current, parent, run, instance, delivery, receipt, saved, state, hold, pane,
                                             report=report_after_unlock, ownership=ownership)
            except ValueError as error:
                _ = after_unlock.callback(report, str(error))
                return TurnOutcome.RETRY_EVIDENCE if event_kind == EventKind.RUN_FINISHED else None
            if not reported:
                _defer_stopped_diagnostics(hold, run, row.id)
            if event_kind == EventKind.RUN_FINISHED and recovery in (
                    worker.CompletionRecovery.PENDING_CLOSE, worker.CompletionRecovery.WAITING_ON_RUNS,
                    worker.CompletionRecovery.WAITING_ON_REPLY, worker.CompletionRecovery.WAITING_ON_REAP):
                return TurnOutcome.RETRY_EVIDENCE
            return None

    return replace(decision, send_notice=send_notice)


def _stopped_pane_use(row: staff.StaffRow, run: Path, receipt: progress_history.StoppedPassReceipt,
                      stopped: dict[str, object], record: staff.StaffSnapshot,
                      ) -> tuple[worker.CloseCompletedPane | worker.ReuseCompletedPane, StoppedPassPane] | worker.DispatchEvidenceUnknown:
    try:
        current_run = worker_panes.worker_run(row, record=record)
        current = worker_panes.worker_pass(row, current_run, record=record)
        current_instance = current.get("instance_id")
        current_start, stopped_start = current.get("started_at"), stopped.get("started_at")
        if (isinstance(current_instance, str) and current_instance and current_instance != receipt.instance_id
                and isinstance(current_start, (int, float)) and isinstance(stopped_start, (int, float))
                and current_start > stopped_start):
            held = progress_history.dispatch_delivery(current).actions.held_role
            return worker.ReuseCompletedPane(held), StoppedPassPane.REPLACED
        if (current_run.resolve() == run.resolve() and current_instance == receipt.instance_id
                and (not receipt.worker_session or receipt.worker_session == row.current_session)):
            return worker.CloseCompletedPane.CLOSE, StoppedPassPane.CURRENT
        if not current and receipt.worker_session and receipt.worker_session == row.current_session:
            last = progress_history.last_dispatch(row.id)
            if isinstance(last, progress_history.DispatchHistoryUnknown):
                return worker.DispatchEvidenceUnknown(last.reason)
            if isinstance(last, progress_history.RecordedDispatch) and last.instance_id == receipt.instance_id:
                return worker.CloseCompletedPane.CLOSE, StoppedPassPane.CURRENT
    except (OSError, ValueError, recorder_state.OldRecorderState) as error:
        return worker.DispatchEvidenceUnknown(str(error))
    return worker.DispatchEvidenceUnknown("the saved session and dispatch do not confirm pane ownership")


def recover_stopped_in_run(run: Path, identity: staff.StaffId, *,
                           hold: worker_panes.WorkerLockHold | worker_panes.WorkerLockAtUse = worker_panes.WorkerLockAtUse.ACQUIRE,
                           ) -> worker.CompletionRecovery:
    """Complete stopped passes by instance under the worker lock."""
    if isinstance(hold, worker_panes.WorkerLockHold):
        _defer_stopped_diagnostics(hold, run, identity)
    receipts = _stopped_diagnostic_receipts(run)
    if isinstance(receipts, StoppedDiagnosticRecovery):
        return worker.CompletionRecovery.PENDING_CLOSE
    selected = [receipt for receipt in receipts if _stopped_receipt_matches(receipt, identity)]
    if not selected:
        if any(isinstance(receipt, progress_history.UnreadableStoppedPassReceipt) for receipt in receipts):
            return worker.CompletionRecovery.PENDING_CLOSE
        return worker.CompletionRecovery.NO_PENDING_NOTICE
    record = staff.snapshot()
    row = record.by_id(identity)
    parent = record.parent_of(row) if row is not None else None
    if row is None or parent is None:
        if isinstance(hold, worker_panes.WorkerLockAtUse):
            _ = retry_stopped_diagnostics(run, identity)
        return worker.CompletionRecovery.PENDING_CLOSE
    with ExitStack() as after_unlock, ExitStack() as operations:
        locked = (operations.enter_context(worker_panes.worker_lock(parent.id, staff.WorkerName.parse(row.name)))
                  if isinstance(hold, worker_panes.WorkerLockAtUse) else hold)
        if isinstance(locked, worker_panes.Kept):
            _ = after_unlock.callback(retry_stopped_diagnostics, run, identity)
            return worker.CompletionRecovery.PENDING_CLOSE
        _defer_stopped_diagnostics(locked, run, identity)
        record = staff.snapshot()
        current = record.by_id(identity)
        if current is None or not locked.owns(current) or not current.reports_to(parent.id):
            return worker.CompletionRecovery.PENDING_CLOSE
        recovery = worker.CompletionRecovery.NO_PENDING_NOTICE
        for receipt in selected:
            passed = progress_history.pass_for_finalization(run, receipt.instance_id)
            completion = progress_history.hook_completion(passed)
            if (not isinstance(completion, progress_history.HookCompletion) or completion.outcome != TurnOutcome.STOPPED
                    or completion.finished):
                continue
            delivery = progress_history.dispatch_delivery(passed)
            use = _stopped_pane_use(current, run, receipt, passed, record)
            if isinstance(use, worker.DispatchEvidenceUnknown):
                return worker.CompletionRecovery.PENDING_CLOSE
            pane, ownership = use
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


def close_interrupted_worker(run: Path, identity: staff.StaffId) -> None:
    """Recover a stopped pass or close its idle pane when no notice is pending."""
    if recover_stopped_in_run(run, identity) is not worker.CompletionRecovery.NO_PENDING_NOTICE:
        return
    record = staff.snapshot()
    row = record.by_id(identity)
    if row is None or row.kind != "worker":
        return
    parent = record.parent_of(row)
    if parent is None:
        return
    name = staff.WorkerName.parse(row.name)
    with worker_panes.worker_lock(parent.id, name, blocking=True) as hold:
        if isinstance(hold, worker_panes.Kept) or not hold.owns(row) or worker_panes.worker_run(row) != run:
            return
        _ = worker_panes.close(parent.id, name, lock=hold)


def _defer_stopped_diagnostics(hold: worker_panes.WorkerLockHold, run: Path, worker_id: staff.StaffId) -> None:
    def deliver() -> None:
        _ = retry_stopped_diagnostics(run, worker_id)

    hold.defer_until_unlock((run, worker_id), deliver)


class StoppedDiagnosticsInRun(Enum):
    ALL = "retry all stopped diagnostics in this run"


def _stopped_diagnostic_receipts(run: Path) -> (
    tuple[progress_history.StoppedPassReceipt | progress_history.UnreadableStoppedPassReceipt, ...]
    | Literal[StoppedDiagnosticRecovery.UNREADABLE]
):
    try:
        return tuple(progress_history.read_stopped_pass_receipts(run))
    except (OSError, ValueError, recorder_state.OldRecorderState) as error:
        unit_runs.report_anomaly(" ".join(f"anomaly: {run}: stopped diagnostic receipts: {error}".splitlines()))
        return StoppedDiagnosticRecovery.UNREADABLE


def _stopped_receipt_matches(receipt: progress_history.StoppedPassReceipt | progress_history.UnreadableStoppedPassReceipt,
                             worker_id: staff.StaffId | StoppedDiagnosticsInRun) -> TypeGuard[progress_history.StoppedPassReceipt]:
    return (isinstance(receipt, progress_history.StoppedPassReceipt)
            and isinstance(receipt.diagnostic_route, progress_history.StoppedDiagnosticRoute)
            and (worker_id is StoppedDiagnosticsInRun.ALL or receipt.diagnostic_route.worker_id == worker_id))


def retry_stopped_diagnostics(run: Path, worker_id: staff.StaffId | StoppedDiagnosticsInRun = StoppedDiagnosticsInRun.ALL,
                              *, propagate_error_for: str = "",
                              ) -> StoppedDiagnosticRecovery:
    """Retry the saved diagnostic receipts after all worker locks release."""
    receipts = _stopped_diagnostic_receipts(run)
    if isinstance(receipts, StoppedDiagnosticRecovery):
        return receipts
    outcome = StoppedDiagnosticRecovery.SETTLED
    reported_errors: list[Exception] = []
    for receipt in receipts:
        if isinstance(receipt, progress_history.UnreadableStoppedPassReceipt):
            unit_runs.report_anomaly(f"anomaly: {run}: stopped receipt {receipt.instance_id} is unreadable")
            outcome = StoppedDiagnosticRecovery.UNREADABLE
            continue
        if not _stopped_receipt_matches(receipt, worker_id):
            continue
        try:
            result = _send_stopped_diagnostics(run, receipt)
        except worker_lifecycle.WorkerStateUnreadable as error:
            unit_runs.report_anomaly(" ".join(f"anomaly: {run}: close diagnostic retry failed: {error}".splitlines()))
            result = StoppedDiagnosticRecovery.UNREADABLE
        except (OSError, ValueError, herdr_types.HerdrFailure, subprocess.SubprocessError, staff.StaffRefused) as error:
            if receipt.instance_id == propagate_error_for:
                reported_errors.append(error)
            else:
                unit_runs.report_anomaly(" ".join(f"anomaly: {run}: close diagnostic retry failed: {error}".splitlines()))
            result = StoppedDiagnosticRecovery.PENDING
        if outcome is StoppedDiagnosticRecovery.SETTLED or result is StoppedDiagnosticRecovery.UNREADABLE:
            outcome = result
    if reported_errors:
        raise reported_errors[0]
    return outcome


def _send_stopped_diagnostics(run: Path, receipt: progress_history.StoppedPassReceipt) -> StoppedDiagnosticRecovery:
    instance = receipt.instance_id
    route = receipt.diagnostic_route
    if isinstance(route, progress_history.MissingStoppedDiagnosticRoute):
        return StoppedDiagnosticRecovery.UNREADABLE
    if pass_notices.has_legacy_diagnostics(run, instance):
        sender_id = route.sender_native_id
        if not sender_id:
            row = staff.snapshot().by_id(route.worker_id)
            if row is not None and receipt.worker_session == row.current_session:
                sender_id = progress_history.stopped_sender_native_id(row)
        # A saved notice needs a sender; the unavailable stamp marks one that no record names.
        sender = sender_id or "unavailable"
        try:
            _ = send.read_sender_stamp(sender)
        except ValueError as error:
            raise worker_lifecycle.WorkerStateUnreadable(pass_recorder.state_path(run), str(error)) from error
        pass_notices.import_legacy_diagnostics(run, pass_notices.Route(instance, route.director_id, route.worker_name),
                                               sender)
    try:
        delivery = pass_notices.deliver_diagnostics(run, instance, busy=pass_notices.BusyDelivery.LEAVE_PENDING)
    except SystemExit as error:
        raise ValueError(f"diagnostic delivery exited with status {error}") from error
    if delivery is pass_notices.DiagnosticsDelivery.PENDING:
        return StoppedDiagnosticRecovery.PENDING
    return StoppedDiagnosticRecovery.SETTLED


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
        if (marker and result is StoppedReplyRecovery.ALREADY_SAVED
                and isinstance(completion, progress_history.HookCompletion)
                and completion.stopped_reply is progress_history.StoppedReplyDisposition.SAVED
                and pass_notices.is_settled(directory, receipt.instance_id, TurnOutcome.STOPPED)):
            return TurnOutcome.STOPPED
        if (isinstance(completion, progress_history.HookCompletion) and not completion.finished
                and completion.outcome == TurnOutcome.STOPPED
                and result in (StoppedReplyRecovery.SAVED, StoppedReplyRecovery.ALREADY_SAVED,
                               StoppedReplyRecovery.UNVERIFIED)):
            if result in (StoppedReplyRecovery.SAVED, StoppedReplyRecovery.ALREADY_SAVED):
                completion = replace(completion, reply_file=str(path), stopped_reply=progress_history.StoppedReplyDisposition.SAVED)
                progress_history.save_hook_completion(directory, receipt.instance_id, completion)
            decision = _stopped_notice(row, parent, directory, passed, completion)
            finalize = _close_stopped_after_notice(row, parent, directory, receipt.instance_id, completion, decision,
                                                  event.kind)
            _ = after_unlock.callback(finalize.send_notice)
        if marker:
            if result not in (StoppedReplyRecovery.SAVED, StoppedReplyRecovery.ALREADY_SAVED):
                return _ignored(event, "the stopped pass reply has no verified DONE receipt", row.name)
            return TurnOutcome.STOPPED
        recovered |= result == StoppedReplyRecovery.SAVED
    return TurnOutcome.STOPPED if recovered else NoLateDone.UNRECORDED


def _save_late_done(event: WorkerEvent, row: staff.StaffRow, artifacts: worker.CompletionArtifacts,
                    passed: dict[str, object],
                    completion: progress_history.HookCompletion | progress_history.NoHookCompletion,
                    delivery: pass_recorder.DispatchDelivery, *, require_transcript: bool,
                    verify: worker.VerifyPassDone | worker.ReadPassDone,
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
    matching_event = (event.kind == EventKind.TURN_END
                      and worker_admission.reply_ends_dispatch(event.reply, delivery.completion_id))
    receipt = worker.dispatch_receipt(row, delivery.line, delivery.completion_id, run=run,
                                      require_transcript=require_transcript, verify=verify)
    if isinstance(receipt, worker.DispatchEvidenceUnknown):
        return receipt
    if isinstance(receipt, worker.DispatchWaitingForRuns) and receipt.has_live_run:
        if matching_event or isinstance(receipt.reply, (worker.DispatchComplete, worker.DispatchAwaitingReply)):
            return TurnOutcome.WAITING_ON_RUNS
        return NoLateDone.UNRECORDED
    if isinstance(receipt, worker.DispatchWaitingForRuns) and receipt.waits_only_for_reap:
        return TurnOutcome.WAITING_ON_REAP
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


def _result_proves_done(event: WorkerEvent, receipt: worker.DispatchReceipt) -> bool:
    """Recover the DONE after a confirmed result delivery in this transcript."""
    if not isinstance(receipt, worker.DispatchComplete) or not isinstance(receipt.result_delivery, worker.ResultDeliveryProof):
        return False
    return receipt.result_delivery.latest_reply.reply == event.reply


def _recover_pass(event: WorkerEvent, row: staff.StaffRow, parent: staff.StaffRow, run: Path, passed: dict[str, object],
                  delivery: pass_recorder.DispatchDelivery, *, require_transcript: bool = False,
                  notice_positions: worker.NoticePositions = worker.NO_NOTICE_POSITIONS,
                  ) -> progress_history.HookCompletion | progress_history.NoHookCompletion | worker.DispatchReceipt | TurnOutcome:
    """Select the saved completion or record a matching DONE under the worker lock."""
    artifacts = worker.CompletionArtifacts(parent, run)
    instance = str(passed["instance_id"])
    stage_one = worker.is_stage_one_review(delivery, instance)
    verify = worker.ReadPassDone.READ if not delivery.completion_id else worker.VerifyPassDone(instance, notice_positions)
    completion = progress_history.hook_completion(passed)
    if isinstance(completion, progress_history.HookCompletion):
        done = completion.outcome in (TurnOutcome.DONE, TurnOutcome.LATE_DONE)
        error_with_done = (completion.outcome == TurnOutcome.ERROR and completion.reply_file
                           and worker_admission.reply_ends_dispatch(_completion_reply(completion), delivery.completion_id))
        if done or error_with_done:
            if not delivery.line or done and completion.finished and event.kind != EventKind.TURN_END:
                return completion
            receipt = worker.dispatch_receipt(row, delivery.line, delivery.completion_id, run=run,
                                              require_transcript=require_transcript, verify=verify)
            if isinstance(receipt, worker.DispatchWaitingForRuns):
                if receipt.waits_only_for_reap:
                    return TurnOutcome.WAITING_ON_REAP
                return TurnOutcome.WAITING_ON_RUNS if receipt.has_live_run else TurnOutcome.WAITING_ON_REPLY
            if isinstance(receipt, worker.DispatchAwaitingReply):
                return TurnOutcome.WAITING_ON_REPLY
            if isinstance(receipt, worker.DispatchEvidenceUnknown):
                return receipt
            if isinstance(receipt, worker.DispatchComplete):
                artifacts.reply(Path(completion.reply_file), receipt.reply)
                return completion
            return receipt
    late = _save_late_done(event, row, artifacts, passed, completion, delivery,
                           require_transcript=require_transcript, verify=verify)
    if not isinstance(late, NoLateDone):
        return late
    if (isinstance(completion, progress_history.HookCompletion)
            or passed.get("status") not in ("active", "refused") or not delivery.line):
        return completion

    receipt = worker.dispatch_receipt(row, delivery.line, delivery.completion_id, run=run,
                                      require_transcript=require_transcript, verify=verify)
    if (not isinstance(receipt, worker.DispatchComplete) or stage_one
            or (event.kind == EventKind.TURN_END and not isinstance(event.native_stop, dict)
                and receipt.reply != event.reply and not _result_proves_done(event, receipt))):
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
                   owner: staff.StaffId | WorkerByThread = WorkerByThread.LOOKUP, *, result_pass: str = "",
                   retry_run: Path | worker_panes.HolderRun = worker_panes.HolderRun.CURRENT,
                   ) -> TurnOutcome | TurnDecision:
    """Recover completion records under the worker lock."""
    record = staff.snapshot()
    row = (record.by_current_session(event.thread_id) if isinstance(owner, WorkerByThread)
           else record.by_id(owner))
    if row is None:
        if not isinstance(owner, WorkerByThread):
            return TurnOutcome.RETRY_EVIDENCE
        _report_missing_worker(event, record)
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
        if (current is None or current.current_session != row.current_session or not hold.owns(current)
                or (event.kind != EventKind.STAGE_READY and current.name != row.name)):
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

        raw_instance = passed.get("instance_id")
        instance = raw_instance if isinstance(raw_instance, str) else ""
        delivery = pass_recorder.dispatch_delivery(passed)
        if result_pass and instance and result_pass != instance:
            return TurnOutcome.IGNORED
        marker = _done_marker(event.reply) if event.kind == EventKind.TURN_END else ""
        stage = delivery.completion_id or instance
        pending_path = worker.pending_done_path(run, row.id)
        pending = worker_lifecycle.read_json_object(pending_path)
        needs_verification = pending.get("instance") == instance and pending.get("stage") == stage
        notice_positions = (worker.NoticePositions(
            latest=worker.TranscriptCursor.from_json(pending.get("latest_notice")),
            delivered=worker.TranscriptCursor.from_json(pending.get("delivered_notice")),
        ) if needs_verification else worker.NO_NOTICE_POSITIONS)
        if needs_verification and "payload" in pending:
            del pending["payload"]
            atomic_file.write_text_atomic(pending_path, json.dumps(pending))
        if isinstance(retry_run, Path):
            if retry_run != run.resolve():
                return TurnOutcome.IGNORED
            if not needs_verification:
                pending_path.unlink(missing_ok=True)
                return TurnOutcome.IGNORED
            last_attempt = pending.get("attempted_at")
            now = time.time()
            delay = conf.read("detached_runs").number("result_push_retry_seconds")
            if isinstance(last_attempt, (int, float)) and 0 <= now - last_attempt < delay:
                return TurnOutcome.RETRY_EVIDENCE
            pending["attempted_at"] = now
            atomic_file.write_text_atomic(pending_path, json.dumps(pending))

        directories = tuple(_stopped_pass_folders(parent, record, run))
        archived_stops = [(directory, receipt) for directory in directories
                          for receipt in progress_history.stopped_pass_receipts(directory, row.id)]

        notice_handled_here = False

        def retry_notices() -> None:
            for directory in directories:
                excluded = instance if notice_handled_here and directory == run else ""
                pass_notices.retry(directory, exclude_instance=excluded)

        _ = after_unlock.callback(retry_notices)
        for directory in dict.fromkeys(directory for directory, _ in archived_stops):
            _defer_stopped_diagnostics(hold, directory, row.id)
        late: TurnOutcome | NoLateDone = NoLateDone.UNRECORDED
        if event.kind == EventKind.TURN_END and not isinstance(event.native_stop, dict) and marker != stage:
            late = _recover_replaced_stops(event, row, parent, run, instance, marker, archived_stops, after_unlock)
            if marker and isinstance(late, TurnOutcome):
                return late

        def ignore_turn(reason: str, *, close_pane: bool = True) -> TurnOutcome:
            if late == TurnOutcome.STOPPED:
                return TurnOutcome.STOPPED
            if not close_pane:
                return _ignored(event, reason, row.name)
            # A run can hold a canceled pass's pane until the run ends.
            stopped = event.kind == EventKind.RUN_FINISHED and passed.get("status") == "canceled"
            if event.kind == EventKind.TURN_END or stopped:
                dispatch = worker_panes.worker_dispatch_state(row, run, hold)
                if not isinstance(dispatch, worker_panes.DispatchHoldsWorker):
                    closed = worker_panes.close(parent.id, staff.WorkerName.parse(row.name), lock=hold)
                    if stopped and not isinstance(closed, worker_panes.Closed):
                        blocker = closed.blocker if isinstance(closed, worker_panes.Kept) else closed.reason
                        reason = f"{reason}; the stopped pane did not close: {blocker}"
                        _ = _ignored(event, reason, row.name)
                        return TurnOutcome.RETRY_EVIDENCE
            return _ignored(event, reason, row.name)

        stopped = progress_history.hook_completion(passed)
        recorded_stop = isinstance(stopped, progress_history.HookCompletion) and stopped.outcome == TurnOutcome.STOPPED
        require_transcript = event.kind == EventKind.RUN_FINISHED or isinstance(event.native_stop, dict)
        state = None
        receipt = None
        if instance and not recorded_stop:
            state = worker_panes.observe_worker(parent.id, row, record)
            receipt = _recover_pass(event, row, parent, run, passed, delivery,
                                    require_transcript=require_transcript, notice_positions=notice_positions)
        verified_done = (isinstance(receipt, progress_history.HookCompletion)
                         and receipt.outcome in (TurnOutcome.DONE, TurnOutcome.LATE_DONE)
                         or isinstance(receipt, worker.DispatchWaitingForRuns)
                         and isinstance(receipt.reply, worker.DispatchComplete))
        if isinstance(event.native_stop, dict) and marker != stage and archived_stops:
            late = _recover_replaced_stops(event, row, parent, run, instance, marker, archived_stops, after_unlock)
            archived_markers = {archived.completion_id for _, archived in archived_stops}
            current_reply = verified_done or isinstance(receipt, (worker.DispatchComplete, worker.DispatchAwaitingReply,
                                                                  worker.DispatchWaitingForRuns)) or (
                isinstance(receipt, worker.DispatchSubmitted) and isinstance(receipt.turn, worker.TranscriptTurn)
                and _done_marker(receipt.turn.reply) not in archived_markers)
            if not current_reply:
                if isinstance(late, TurnOutcome):
                    return late
                if not event.reply and not needs_verification and isinstance(receipt, worker.DispatchSubmitted):
                    return _ignored(event, "the replacement pass has no transcript reply", row.name)
        if (instance and delivery.line and not recorded_stop and isinstance(event.native_stop, dict)
                and not verified_done and not isinstance(receipt, worker.DispatchComplete)):
            if not needs_verification:
                pending.clear()
                pending.update(instance=instance, stage=stage, attempted_at=time.time())
                atomic_file.write_text_atomic(pending_path, json.dumps(pending))
            needs_verification = True
        if needs_verification and isinstance(receipt, (worker.DispatchEvidenceUnknown, worker.DispatchNotSubmitted)):
            return TurnOutcome.RETRY_EVIDENCE
        if not verified_done and event.kind in (EventKind.TURN_END, EventKind.RUN_FINISHED):
            worker_input = (detached_runs.WorkerResultInput.TURN_ENDED if event.kind == EventKind.TURN_END
                            else detached_runs.WorkerResultInput.TYPE)
            parked = progress_history.parked_worker_pass(passed)
            resume = (lambda: worker.restore_parked_worker(row, parent.id, hold)) if parked else detached_runs.ParkedWorkerDelivery.DEFER
            replay = detached_runs.submit_finished_worker_result(
                run, row, worker_input=worker_input, resume_parked=resume, input_lock=hold)
            if isinstance(replay, detached_runs.ConfirmedPaneResult):
                if replay.ownership is detached_runs.ResultPassOwnership.CURRENT:
                    if parked and not worker.clear_result_park(row, worker_panes.ParkWaitingPass(
                            run, instance, delivery.completion_id)):
                        return TurnOutcome.RETRY_EVIDENCE
                return TurnOutcome.SUBMITTED
            if replay in (detached_runs.WorkerResultReplay.RETRY, detached_runs.WorkerResultReplay.DUPLICATE):
                return TurnOutcome.RETRY_EVIDENCE
        if not instance:
            return ignore_turn("no active dispatch or recoverable completion exists")
        if isinstance(stopped, progress_history.HookCompletion) and recorded_stop:
            if passed.get("status") == "canceled":
                receipt = progress_history.stopped_pass_receipt(run, passed)
                if event.kind == EventKind.RUN_FINISHED or (
                        event.kind == EventKind.TURN_END and marker in ("", receipt.completion_id)):
                    recovered = _record_stopped_reply(row, parent, run, receipt, stopped)
                    if isinstance(recovered, progress_history.HookCompletion):
                        stopped = recovered
                    elif isinstance(receipt.results, progress_history.LegacyResultsForStoppedPass):
                        if isinstance(recovered, worker.DispatchEvidenceUnknown):
                            return TurnOutcome.RETRY_EVIDENCE
                        if recovered == StoppedReplyRecovery.WAITING_ON_RUNS:
                            return TurnOutcome.WAITING_ON_RUNS
                        if recovered == StoppedReplyRecovery.WAITING_ON_REPLY:
                            return TurnOutcome.WAITING_ON_REPLY
                        if recovered == StoppedReplyRecovery.WAITING_ON_REAP:
                            return TurnOutcome.WAITING_ON_REAP
                    decision = _stopped_notice(row, parent, run, passed, stopped)
                    notice_handled_here = True
                    return _close_stopped_after_notice(row, parent, run, instance, stopped, decision, event.kind)
                notice_handled_here = True
                return _stopped_notice(row, parent, run, passed, stopped)
            return _ignored(event, "the recorder has not ended the stopped pass", row.name)
        client = herdr.Herdr(herdr.shared_session())
        assert state is not None and receipt is not None
        if isinstance(receipt, TurnOutcome):
            return receipt
        if isinstance(receipt, progress_history.HookCompletion):
            if receipt.finished:
                if needs_verification:
                    pending_path.unlink(missing_ok=True)
                if receipt.pane_closed and receipt.outcome in (TurnOutcome.DONE, TurnOutcome.LATE_DONE):
                    detached_runs.stage_worker_stop_results(run, row, instance)
                    if detached_runs.acknowledge_worker_completion(run, row, passed) == detached_runs.CompletionReplay.RETRY:
                        return TurnOutcome.RETRY_EVIDENCE
                if isinstance(state, worker_panes.ClosedWorker):
                    _remove_completed_restart(row, delivery)
                return ignore_turn("the hook completion is already finished")
            notice_handled_here = True
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
                notice_handled_here = True
                return _submit_stage(parent, row, state, client, run, passed, instance, delivery, hold)
            return ignore_turn("the transcript has no matching dispatch prompt")
        if isinstance(receipt, worker.DispatchWaitingForRuns) and receipt.waits_only_for_reap:
            return TurnOutcome.WAITING_ON_REAP
        turn_reply = receipt.reply if isinstance(receipt, worker.DispatchWaitingForRuns) else receipt
        reply = turn_reply.last_reply if isinstance(turn_reply, worker.DispatchAwaitingReply) else turn_reply.reply
        if (needs_verification and marker and marker == stage and (
                isinstance(turn_reply, worker.DispatchSubmitted)
                or isinstance(turn_reply, worker.DispatchAwaitingReply)
                and isinstance(turn_reply.matching_done, worker.NoMatchingDone))):
            return TurnOutcome.RETRY_EVIDENCE
        if (event.kind == EventKind.RUN_FINISHED and isinstance(receipt, worker.DispatchWaitingForRuns)
                and not receipt.has_live_run and isinstance(turn_reply, worker.DispatchSubmitted) and not reply):
            return TurnOutcome.WAITING_ON_REPLY
        if (event.kind == EventKind.RUN_FINISHED and not isinstance(receipt, worker.DispatchComplete)
                and isinstance(state, worker_panes.OpenWorker) and state.status == herdr.AgentStatus.WORKING):
            return TurnOutcome.WAITING_ON_REPLY
        turn_cursor: worker.TranscriptCursor | worker.NoTranscriptTurn = worker.NoTranscriptTurn.UNRECORDED
        if isinstance(receipt, worker.DispatchSubmitted):
            empty_turn_eligible = (receipt.compacted_after_prompt or (
                isinstance(state, worker_panes.OpenWorker) and state.status.idle
                and (not archived_stops or passed.get("submitted_worker_thread") == event.thread_id))
                ) and passed.get("status") in ("active", "refused")
            if needs_verification:
                if not isinstance(receipt.turn, worker.TranscriptTurn):
                    return TurnOutcome.RETRY_EVIDENCE
                reply = receipt.turn.reply
                turn_cursor = receipt.turn.cursor
                position = asdict(turn_cursor)
                delivered = receipt.turn.delivered
                if pending.get("latest_notice") != position or delivered != notice_positions.delivered:
                    pending["latest_notice"] = position
                    if isinstance(delivered, worker.TranscriptCursor):
                        pending["delivered_notice"] = asdict(delivered)
                    else:
                        _ = pending.pop("delivered_notice", None)
                    atomic_file.write_text_atomic(pending_path, json.dumps(pending))
                if not reply and not empty_turn_eligible:
                    return TurnOutcome.RETRY_EVIDENCE
            elif event.kind == EventKind.TURN_END and not event.reply and empty_turn_eligible:
                notice_handled_here = True
                return _prepare_notice(row.name, parent, run, passed, instance, TurnOutcome.NO_DONE,
                                       Path(delivery.reply_file), "", "", source_event_id=event.source_event_id)
        if isinstance(receipt, worker.DispatchSubmitted) and not reply and not needs_verification:
            if event.kind == EventKind.STAGE_READY:
                return TurnOutcome.SUBMITTED
            return ignore_turn("the submitted dispatch has no final reply", close_pane=False)
        anomalies = receipt.anomalies if isinstance(receipt, worker.DispatchComplete) else ()
        if (event.kind == EventKind.TURN_END and (not reply or reply != event.reply)
                and not isinstance(event.native_stop, dict)
                and not isinstance(turn_cursor, worker.TranscriptCursor)
                and not _result_proves_done(event, receipt)
                and not (isinstance(receipt, worker.DispatchWaitingForRuns) and
                         isinstance(receipt.reply, (worker.DispatchComplete, worker.DispatchAwaitingReply)))
                and not (needs_verification and isinstance(receipt, worker.DispatchAwaitingReply))
                and not (isinstance(receipt, worker.DispatchWaitingForRuns) and receipt.can_park
                         and not reply and not event.reply)):
            reason = "the transcript reply is empty" if not reply else "the transcript reply differs from the turn reply"
            return ignore_turn(reason, close_pane=False)
        park_event = event.kind == EventKind.TURN_END or (
            event.kind == EventKind.RUN_FINISHED and progress_history.parked_worker_pass(passed))
        if (park_event and isinstance(receipt, worker.DispatchWaitingForRuns)
                and receipt.can_park and passed.get("status") in ("active", "refused")
                and not (delivery.early_ready and delivery.completion_id == instance)
                and isinstance(state, worker_panes.OpenWorker) and state.status.idle):
            closed = worker.park_waiting_worker(row, hold, worker_panes.ParkWaitingPass(run, instance, delivery.completion_id),
                                               delivery, receipt)
            if not isinstance(closed, worker_panes.Closed):
                _log_hook_event(event, _kept_pane_anomaly(row, state,
                    closed.blocker if isinstance(closed, worker_panes.Kept) else closed.reason), row.name)
        stale_done = _stale_done_detail(reply, delivery.completion_id)
        if isinstance(receipt, worker.DispatchComplete):
            progress_history.confirm_assignment_submission(run, instance, delivery.completion_id,
                                                            worker_thread=str(row.current_session))
            reply = receipt.reply
            if delivery.early_ready and delivery.completion_id == instance:
                path = Path(delivery.reply_file)
                worker.save_reply(path.with_name(f"{path.stem}_stage1_{instance}{path.suffix}"), reply)
            if delivery.early_ready and Path(delivery.early_ready).exists():
                completion_id = worker_panes.stage_two_completion_id(instance)
                diff = delivery.early_ready.removesuffix(".ready") + ".diff"
                line = (f"Stage 2: the final diff is at {json.dumps(diff)}. "
                        + delivery.line.replace(f"DONE {instance}", f"DONE {completion_id}"))
                delivery = replace(delivery, line=line, completion_id=completion_id)
                progress_history.save_dispatch_delivery(run, instance, delivery)
                if needs_verification:
                    pending_path.unlink(missing_ok=True)
                notice_handled_here = True
                return _submit_stage(parent, row, state, client, run, passed, instance, delivery, hold)
            outcome = TurnOutcome.WAITING_ON_STAGE_TWO
        elif isinstance(receipt, worker.DispatchWaitingForRuns):
            outcome = TurnOutcome.WAITING_ON_RUNS if receipt.has_live_run else TurnOutcome.WAITING_ON_REPLY
        elif isinstance(receipt, worker.DispatchAwaitingReply):
            outcome = TurnOutcome.WAITING_ON_REPLY
        elif (not stale_done and (reply or not isinstance(turn_cursor, worker.TranscriptCursor))
              and _friend_pending(client, state, row)):
            outcome = TurnOutcome.WAITING_ON_FRIEND
        else:
            outcome = TurnOutcome.NO_DONE
        path = Path(delivery.reply_file)
        if reply or not isinstance(turn_cursor, worker.TranscriptCursor):
            path = path.with_name(path.stem + "_turn" + path.suffix)
        if outcome == TurnOutcome.NO_DONE and isinstance(turn_cursor, worker.TranscriptCursor):
            if pending.get("delivered_notice") == asdict(turn_cursor):
                return TurnOutcome.NO_DONE
        else:
            turn_cursor = worker.NoTranscriptTurn.UNRECORDED
        if reply:
            worker.CompletionArtifacts(parent, run).reply(path, reply)
        if outcome == TurnOutcome.NO_DONE and delivery.actions.end_worker and not stale_done and not needs_verification:
            completion = progress_history.HookCompletion(outcome, str(path), anomalies)
            progress_history.save_hook_completion(run, instance, completion)
            notice_handled_here = True
            return _finish_completion(row, parent, run, passed, instance, delivery, completion, state)
        details = stale_done if outcome == TurnOutcome.NO_DONE and stale_done else "\n".join(anomalies) or reply
        runs = (tuple(run_id for run_id in receipt.runs if run_id not in receipt.reap_required)
                if isinstance(receipt, worker.DispatchWaitingForRuns) else ())
        notice_handled_here = True
        return _prepare_notice(row.name, parent, run, passed, instance, outcome, path, reply, details, runs,
                               source_event_id=event.source_event_id, turn_cursor=turn_cursor)


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


def completion_close_pending(owner: staff.StaffId, run: Path | worker_panes.HolderRun = worker_panes.HolderRun.CURRENT,
                             *, result_pass: str = "") -> bool:
    record = staff.snapshot()
    row = record.by_id(owner)
    if row is None or row.kind != "worker":
        return False
    directory = worker_panes.worker_run(row, record=record) if isinstance(run, worker_panes.HolderRun) else run
    passed = worker_panes.worker_pass(row, directory, record=record)
    current = passed.get("instance_id")
    if result_pass and isinstance(current, str) and current and current != result_pass:
        return False
    completion = progress_history.hook_completion(passed)
    if not isinstance(completion, progress_history.HookCompletion):
        return False
    return not (completion.pane_closed if completion.outcome == TurnOutcome.STOPPED else completion.finished)


def run_finished(owner: staff.StaffId, result_pass: str = "") -> detached_runs.CompletionReplay:
    """Keep the run completion available when the transcript is unavailable."""
    outcome = _handle_event(WorkerEvent("", EventKind.RUN_FINISHED), owner, result_pass=result_pass)
    if outcome in (TurnOutcome.UNCERTAIN, TurnOutcome.RETRY_EVIDENCE, TurnOutcome.SUBMITTED,
                   TurnOutcome.WAITING_ON_RUNS, TurnOutcome.WAITING_ON_REPLY, TurnOutcome.WAITING_ON_REAP):
        return detached_runs.CompletionReplay.RETRY
    if completion_close_pending(owner, result_pass=result_pass):
        return detached_runs.CompletionReplay.RETRY
    return detached_runs.CompletionReplay.HANDLED


def retry_pending_done(run: Path, worker_name: str = "") -> None:
    """Retry pending transcript reads during detached-run reap."""
    paths = list(run.glob(".*.pending-done.json"))
    if not paths:
        return
    record = staff.snapshot()
    for path in paths:
        identity = staff.StaffId(path.name.removeprefix(".").removesuffix(".pending-done.json"))
        row = record.by_id(identity)
        if row is None or row.kind != "worker" or (worker_name and worker_name != row.name):
            continue
        try:
            _ = _handle_event(WorkerEvent("", EventKind.RUN_FINISHED), identity, retry_run=run.resolve())
        except (OSError, ValueError, staff.StaffUnreadable, staff.StaffRefused, herdr_types.HerdrFailure) as error:
            unit_runs.report_anomaly(f"anomaly: {row.name}: DONE verification retry failed: {error}")


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

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
from typing import Callable, cast

from scripts.agents import detached_runs, remote, shutdown_record, worker_lifecycle
from scripts.direct import pass_record as pass_recorder
from scripts.direct import progress_history, recorder_state
from scripts.herdr import herdr_types
from scripts.lib import atomic_file, conf, scratch, unit_runs
from scripts.herdr import herdr, pass_notices, worker_panes
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
                    source_event_id: str = "",
                    turn_cursor: worker.TranscriptCursor | worker.NoTranscriptTurn = worker.NoTranscriptTurn.UNRECORDED,
                    ) -> TurnDecision:
    """Save the result before it can leave the worker pass."""
    busy = outcome == TurnOutcome.BUSY
    phase = pass_recorder.object_dict(passed.get("phase")) or {}
    result_text = "the dispatch attempt found the worker busy" if busy else outcome.value
    message = f"{worker_name}: phase {phase.get('id', '')}: {result_text}"
    if reply:
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
        pass_notices.stage_result(run, route, notice_type, notice, legacy=legacy)
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
    return _prepare_notice(row.name, parent, run, passed, str(passed["instance_id"]), TurnOutcome.STOPPED,
                           Path(), "", "\n".join(completion.details), notice_type=TurnOutcome.STOPPED,
                           defer_notice=defer_notice)


def _recorded_stop_notice(run: Path, notice: progress_history.StoppedPassNotice) -> worker_panes.StoppedRunPassCompletion:
    record = staff.snapshot()
    director = record.by_id(notice.director_id)
    settled = pass_notices.is_settled(run, notice.instance_id, TurnOutcome.STOPPED)
    if director is None:
        if settled:
            return worker_panes.StoppedRunPassCompletion(TurnOutcome.STOPPED, _no_notice)
        raise ValueError(f"{notice.director_id}: stopped pass notice recipient is not in staff")
    decision = TurnDecision(TurnOutcome.STOPPED, _no_notice)
    if not settled:
        passed: dict[str, object] = {"worker_id": notice.worker_id, "phase": {"id": notice.phase_id}}
        decision = _prepare_notice(notice.worker_name, director, run, passed, notice.instance_id,
                                   TurnOutcome.STOPPED, Path(), "", notice.reason, notice_type=TurnOutcome.STOPPED)
    row = record.by_id(notice.worker_id)
    if row is not None and row.reports_to(director.id):
        passed = worker_panes.worker_pass(row, run, record=record)
        completion = progress_history.hook_completion(passed)
        if (passed.get("instance_id") == notice.instance_id and isinstance(completion, progress_history.HookCompletion)
                and completion.outcome == TurnOutcome.STOPPED):
            decision = _close_stopped_after_notice(row, director, run, notice.instance_id, completion, decision)
    return worker_panes.StoppedRunPassCompletion(decision.outcome, decision.send_notice)


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
            if passed.get("status") == "canceled":
                return worker_panes.StoppedRunPassCompletion(
                    TurnOutcome.STOPPED, _stopped_notice(current, parent, run, passed, completion).send_notice)
        if (passed.get("status") == "completed" and isinstance(instance, str)
                and isinstance(completion, progress_history.HookCompletion) and completion.outcome == TurnOutcome.DONE):
            if completion.finished:
                return worker_panes.StoppedRunPassCompletion(TurnOutcome.DONE, _no_notice)
            event = WorkerEvent(str(current.current_session), EventKind.RUN_FINISHED)
            recovered = _recover_pass(event, current, run, passed, delivery, require_transcript=True)
            if not isinstance(recovered, progress_history.HookCompletion):
                return worker_panes.Kept("the pass awaits verified completion")
            state = worker_panes.observe_worker(parent.id, current, record)
            decision = _finish_completion(current, parent, run, passed, instance, delivery, recovered, state)
            return worker_panes.StoppedRunPassCompletion(decision.outcome, decision.send_notice)
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
            receipt = _recover_pass(event, current, run, passed, delivery, require_transcript=True)
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
        return worker_panes.StoppedRunPassCompletion(decision.outcome, decision.send_notice)


def retry_completed_notice(worker_name: str, parent: staff.StaffRow, run: Path,
                           passed: dict[str, object]) -> Callable[[], None]:
    """Retry the saved result after the worker lock is released."""
    instance = str(passed["instance_id"])
    completion = progress_history.hook_completion(passed)
    delivery = pass_recorder.dispatch_delivery(passed)
    outcome = TurnOutcome(completion.outcome) if isinstance(completion, progress_history.HookCompletion) else TurnOutcome.DONE
    path = Path(completion.reply_file if isinstance(completion, progress_history.HookCompletion) else delivery.reply_file)
    details = _completion_notice_details(completion) if isinstance(completion, progress_history.HookCompletion) else ""
    reply = _completion_reply(completion) if isinstance(completion, progress_history.HookCompletion) else path.read_text()
    if delivery.actions.status_file:
        atomic_file.write_text_atomic(Path(delivery.actions.status_file), "done\n")

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
    worker.save_reply(path, details)
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
        result = assign.complete_herdr(run, row, passed, delivery.actions, path,
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
                         delivery: pass_recorder.DispatchDelivery, completion: progress_history.HookCompletion,
                         state: worker_panes.WorkerPane | worker_panes.WorkerNotObserved,
                         hold: worker_panes.WorkerLockHold, pane: worker.CloseCompletedPane | worker.ReuseCompletedPane,
                         ) -> TurnOutcome:
    from scripts.direct import assign

    diagnostics: list[str] = []

    try:
        name = staff.WorkerName.parse(row.name)
        if isinstance(pane, worker.ReuseCompletedPane) and not completion.pane_closed:
            if completion.outcome in (TurnOutcome.DONE, TurnOutcome.LATE_DONE):
                evidence = worker.completion_close_evidence(row, delivery, run=run, reply_file=completion.reply_file)
                if isinstance(evidence, worker_panes.Kept):
                    raise ValueError(evidence.blocker)
            completion = replace(completion, pane_closed=True,
                                 role_released=completion.role_released or delivery.actions.held_role == pane.held_role)
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
    if not completion.recording_done:
        result = assign.finish_herdr_recording(run, delivery.actions,
            completed=completion.outcome == TurnOutcome.DONE and completion.pane_closed)
        if isinstance(result, assign.CompletionFailed):
            diagnostics.append(result.reason if outcome == TurnOutcome.DONE else f"{outcome}: {result.reason}")
            outcome = TurnOutcome.ERROR
    pending = tuple(dict.fromkeys((*completion.pending_diagnostics, *diagnostics)))
    if pending != completion.pending_diagnostics:
        completion = replace(completion, pending_diagnostics=pending)
        progress_history.save_hook_completion(run, instance, completion)
    route = pass_notices.Route(instance, parent.id, row.name)
    if isinstance(pane, worker.CloseCompletedPane):
        pass_notices.import_legacy_diagnostics(run, route, completion.sender_native_id)
    _ = pass_notices.stage_diagnostics(run, route, completion.sender_native_id, pending)
    details = tuple(dict.fromkeys((*completion.details, *pending)))
    completion = replace(completion, details=details, recording_done=True, pending_diagnostics=())
    progress_history.save_hook_completion(run, instance, completion)
    if isinstance(pane, worker.CloseCompletedPane):
        pass_notices.deliver_diagnostics(run, instance)
    if completion.pane_closed and completion.role_released:
        progress_history.save_hook_completion(run, instance, replace(completion, finished=True))
        pending = worker.pending_done_path(run, row.id)
        if worker_lifecycle.read_json_object(pending).get("instance") == instance:
            pending.unlink(missing_ok=True)
        if completion.outcome in (TurnOutcome.DONE, TurnOutcome.LATE_DONE):
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
    passed = worker_panes.worker_pass(row, run, record=record)
    instance = str(passed.get("instance_id", ""))
    if not instance:
        return worker.CompletionRecovery.NO_PENDING_NOTICE
    stopped = progress_history.hook_completion(passed)
    if isinstance(stopped, progress_history.HookCompletion) and stopped.outcome == TurnOutcome.STOPPED:
        if passed.get("status") == "canceled":
            receipt = _stopped_receipt(instance, pass_recorder.dispatch_delivery(passed))
            recovered = _record_stopped_reply(row, run, receipt, stopped)
            _ = _stopped_notice(row, parent, run, passed, stopped,
                                defer_notice=not isinstance(pane, worker.CloseCompletedPane)).send_notice()
            if isinstance(pane, worker.ReuseCompletedPane):
                return worker.CompletionRecovery.NO_PENDING_NOTICE
            if isinstance(recovered, worker.DispatchEvidenceUnknown):
                raise ValueError(recovered.reason)
            if recovered == StoppedReplyRecovery.WAITING_ON_RUNS:
                return worker.CompletionRecovery.WAITING_ON_RUNS
            if recovered == StoppedReplyRecovery.WAITING_ON_REPLY:
                return worker.CompletionRecovery.WAITING_ON_REPLY
            if recovered == StoppedReplyRecovery.WAITING_ON_REAP:
                return worker.CompletionRecovery.WAITING_ON_REAP
        return worker.CompletionRecovery.NO_PENDING_NOTICE
    delivery = pass_recorder.dispatch_delivery(passed)
    event = WorkerEvent(str(row.current_session), EventKind.RUN_FINISHED)
    completion = _recover_pass(event, row, run, passed, delivery, require_transcript=True)
    if isinstance(completion, worker.DispatchEvidenceUnknown):
        raise ValueError(completion.reason)
    if completion == TurnOutcome.WAITING_ON_RUNS:
        return worker.CompletionRecovery.WAITING_ON_RUNS
    if completion == TurnOutcome.WAITING_ON_REAP or (
            isinstance(completion, worker.DispatchWaitingForRuns) and completion.waits_only_for_reap):
        return worker.CompletionRecovery.WAITING_ON_REAP
    if completion == TurnOutcome.WAITING_ON_REPLY or isinstance(completion, worker.DispatchAwaitingReply):
        return worker.CompletionRecovery.WAITING_ON_REPLY
    if isinstance(completion, worker.DispatchWaitingForRuns) and not completion.has_live_run:
        return worker.CompletionRecovery.WAITING_ON_REPLY
    if (isinstance(completion, worker.DispatchWaitingForRuns) and completion.has_live_run
            and isinstance(completion.reply, (worker.DispatchComplete, worker.DispatchAwaitingReply))):
        return worker.CompletionRecovery.WAITING_ON_RUNS
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


def _stopped_receipt(instance: str, delivery: pass_recorder.DispatchDelivery) -> progress_history.StoppedPassReceipt:
    return progress_history.StoppedPassReceipt(instance, delivery.line, delivery.completion_id or instance,
                                               delivery.reply_file)


class StoppedReplyRecovery(Enum):
    SAVED = "saved the verified reply"
    ALREADY_SAVED = "the file contains the latest verified reply"
    UNVERIFIED = "no verified DONE for the stopped pass"
    WAITING_ON_RUNS = worker.CompletionRecovery.WAITING_ON_RUNS.value
    WAITING_ON_REPLY = worker.CompletionRecovery.WAITING_ON_REPLY.value
    WAITING_ON_REAP = worker.CompletionRecovery.WAITING_ON_REAP.value


def _save_stopped_reply(row: staff.StaffRow, run: Path,
                        receipt: progress_history.StoppedPassReceipt,
                        ) -> StoppedReplyRecovery | worker.DispatchEvidenceUnknown:
    if not receipt.line:
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
    path = _late_reply_path(row, run, receipt.reply_file, receipt.instance_id)
    try:
        if path.read_text(encoding="utf-8").rstrip() == reply.reply.rstrip():
            return StoppedReplyRecovery.ALREADY_SAVED
    except FileNotFoundError:
        pass
    worker.save_reply(path, reply.reply)
    return StoppedReplyRecovery.SAVED


def _record_stopped_reply(row: staff.StaffRow, run: Path, receipt: progress_history.StoppedPassReceipt,
                          completion: progress_history.HookCompletion,
                          ) -> progress_history.HookCompletion | StoppedReplyRecovery | worker.DispatchEvidenceUnknown:
    recovered = _save_stopped_reply(row, run, receipt)
    if recovered not in (StoppedReplyRecovery.SAVED, StoppedReplyRecovery.ALREADY_SAVED):
        return recovered
    path = _late_reply_path(row, run, receipt.reply_file, receipt.instance_id)
    if completion.reply_file != str(path):
        completion = replace(completion, reply_file=str(path))
        progress_history.save_hook_completion(run, receipt.instance_id, completion)
    return completion


def _close_stopped_after_notice(row: staff.StaffRow, parent: staff.StaffRow, run: Path, instance: str,
                                completion: progress_history.HookCompletion, decision: TurnDecision) -> TurnDecision:
    def report(reason: str) -> None:
        message = f"{row.name}: kept pane: {' '.join(reason.splitlines())}"
        print(message, file=sys.stderr)
        _send_stopped_diagnostics(row, parent, run, instance, (message,), sender_native_id=completion.sender_native_id)

    def send_notice() -> None:
        _ = decision.send_notice()
        name = staff.WorkerName.parse(row.name)
        with ExitStack() as after_unlock, worker_panes.worker_lock(parent.id, name, blocking=True) as hold:
            if isinstance(hold, worker_panes.Kept):
                _ = after_unlock.callback(report, hold.blocker)
                return
            record = staff.snapshot()
            current = record.by_id(row.id)
            if (current is None or current.name != row.name or current.current_session != row.current_session
                    or not hold.owns(current) or worker_panes.worker_run(current, record=record) != run):
                return
            passed = worker_panes.worker_pass(current, run, record=record)
            saved = progress_history.hook_completion(passed)
            if (passed.get("instance_id") != instance or passed.get("status") != "canceled"
                    or not isinstance(saved, progress_history.HookCompletion)
                    or not _same_completion(saved, completion)
                    or not pass_notices.is_settled(run, instance, TurnOutcome.STOPPED)):
                return
            if saved.pane_closed:
                return
            receipt = _stopped_receipt(instance, pass_recorder.dispatch_delivery(passed))
            recovered = _record_stopped_reply(current, run, receipt, saved)
            try:
                with redirect_stderr(io.StringIO()):
                    closed = worker_panes.close(parent.id, name, lock=hold)
            except (OSError, ValueError, herdr_types.HerdrFailure, subprocess.SubprocessError,
                    staff.StaffRefused, staff.StaffUnreadable) as error:
                _ = after_unlock.callback(report, str(error))
                return
            if isinstance(closed, worker_panes.Closed):
                latest = recovered if isinstance(recovered, progress_history.HookCompletion) else saved
                progress_history.save_hook_completion(run, instance, replace(latest, pane_closed=True))
            elif isinstance(closed, worker_panes.UncertainWorker) or isinstance(recovered, progress_history.HookCompletion):
                _ = after_unlock.callback(report, closed.blocker if isinstance(closed, worker_panes.Kept) else closed.reason)

    return replace(decision, send_notice=send_notice)


def _send_stopped_diagnostics(row: staff.StaffRow, parent: staff.StaffRow, run: Path, instance: str,
                              messages: tuple[str, ...] = (), *, sender_native_id: str = "") -> None:
    route = pass_notices.Route(instance, parent.id, row.name)
    keys = pass_notices.stage_diagnostics(run, route, sender_native_id or _native_sender(row), messages)
    pass_notices.deliver(run, instance, keys)


def _stopped_pass_folders(parent: staff.StaffRow, record: staff.StaffSnapshot, run: Path) -> Generator[Path]:
    yield run
    held = unit_runs.held(str(parent.current_session), record=record, scanned=worker_panes.parent_run_index(record))
    recorded = (parent.run.path,) if isinstance(parent.run, staff.OpenRun) else ()
    fallback = worker_panes.worker_fallback_run(parent, record=record)
    for directory in dict.fromkeys((*(Path(folder) for folder in held), *recorded, fallback)):
        if directory != run:
            yield directory


def _recover_replaced_stops(event: WorkerEvent, row: staff.StaffRow, run: Path, instance: str, marker: str,
                            archived_stops: Iterable[tuple[Path, progress_history.StoppedPassReceipt]],
                            ) -> TurnOutcome | NoLateDone:
    recovered = False
    for directory, receipt in archived_stops:
        if directory == run and receipt.instance_id == instance:
            continue
        if marker and receipt.completion_id != marker:
            continue
        path = _late_reply_path(row, directory, receipt.reply_file, receipt.instance_id)
        try:
            if not marker:
                try:
                    if worker_admission.reply_ends_dispatch(path.read_text(encoding="utf-8"), receipt.completion_id):
                        continue
                except FileNotFoundError:
                    pass
            result = _save_stopped_reply(row, directory, receipt)
        except (OSError, UnicodeError) as error:
            reason = " ".join(f"anomaly: unable to recover stopped pass reply {path}: {error}".splitlines())
            unit_runs.report_anomaly(reason)
            _log_hook_event(event, reason, row.name)
            continue
        if marker:
            if result not in (StoppedReplyRecovery.SAVED, StoppedReplyRecovery.ALREADY_SAVED):
                return _ignored(event, "the stopped pass reply has no verified DONE receipt", row.name)
            return TurnOutcome.STOPPED
        recovered |= result == StoppedReplyRecovery.SAVED
    return TurnOutcome.STOPPED if recovered else NoLateDone.UNRECORDED


def _save_late_done(event: WorkerEvent, row: staff.StaffRow, run: Path, passed: dict[str, object],
                    completion: progress_history.HookCompletion | progress_history.NoHookCompletion,
                    delivery: pass_recorder.DispatchDelivery, *, require_transcript: bool,
                    verify: worker.VerifyPassDone | worker.ReadPassDone,
                    ) -> progress_history.HookCompletion | worker.DispatchEvidenceUnknown | TurnOutcome | NoLateDone:
    """Save the late reply and notice outcome for a pass the recorder already ended."""
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
    worker.save_reply(path, receipt.reply)
    details = (f"The earlier pass ended with state {earlier}.", *receipt.anomalies)
    if earlier == "error" and (not isinstance(completion, progress_history.HookCompletion) or not completion.finished):
        if isinstance(completion, progress_history.HookCompletion):
            details += completion.details
        if delivery.reply_file:
            path = Path(delivery.reply_file)
            worker.save_reply(path, receipt.reply)
        saved = progress_history.HookCompletion(TurnOutcome.DONE, str(path), details)
    else:
        if earlier == "completed" and delivery.reply_file:
            destination = Path(delivery.reply_file)
            try:
                recorded_reply = destination.read_text()
            except FileNotFoundError:
                worker.save_reply(destination, receipt.reply)
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


def _recover_pass(event: WorkerEvent, row: staff.StaffRow, run: Path, passed: dict[str, object],
                  delivery: pass_recorder.DispatchDelivery, *, require_transcript: bool = False,
                  notice_positions: worker.NoticePositions = worker.NO_NOTICE_POSITIONS,
                  ) -> progress_history.HookCompletion | progress_history.NoHookCompletion | worker.DispatchReceipt | TurnOutcome:
    """Select the saved completion or record a matching DONE under the worker lock."""
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
                worker.save_reply(Path(completion.reply_file), receipt.reply)
                return completion
            return receipt
    late = _save_late_done(event, row, run, passed, completion, delivery,
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
    worker.save_reply(path, receipt.reply)
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
        if not isinstance(owner, WorkerByThread):
            event = replace(event, thread_id=str(row.current_session))
        run = worker_panes.worker_run(row, record=record)
        passed = worker_panes.worker_pass(row, run, record=record)

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
            delay = conf.read("detached_runs").number("RESULT_PUSH_RETRY_SECONDS")
            if isinstance(last_attempt, (int, float)) and 0 <= now - last_attempt < delay:
                return TurnOutcome.RETRY_EVIDENCE
            pending["attempted_at"] = now
            atomic_file.write_text_atomic(pending_path, json.dumps(pending))

        directories = tuple(_stopped_pass_folders(parent, record, run))
        archived_stops = [(directory, receipt) for directory in directories
                          for receipt in progress_history.stopped_pass_receipts(directory, row.id)]

        notice_handled_here = False
        retry_stopped_diagnostics = False

        def retry_notices() -> None:
            for directory, receipt in archived_stops:
                try:
                    route = pass_notices.Route(receipt.instance_id, parent.id, row.name)
                    pass_notices.import_legacy_diagnostics(directory, route, _native_sender(row))
                except (OSError, ValueError, staff.StaffRefused) as error:
                    unit_runs.report_anomaly(
                        " ".join(f"anomaly: {row.name}: old diagnostic recovery failed: {error}".splitlines()))
            for directory in directories:
                excluded = instance if notice_handled_here and directory == run else ""
                pass_notices.retry(directory, exclude_instance=excluded,
                                   retry_excluded_diagnostics=retry_stopped_diagnostics)

        _ = after_unlock.callback(retry_notices)
        late: TurnOutcome | NoLateDone = NoLateDone.UNRECORDED
        if event.kind == EventKind.TURN_END and not isinstance(event.native_stop, dict) and marker != stage:
            late = _recover_replaced_stops(event, row, run, instance, marker, archived_stops)
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
            receipt = _recover_pass(event, row, run, passed, delivery,
                                    require_transcript=require_transcript, notice_positions=notice_positions)
        verified_done = (isinstance(receipt, progress_history.HookCompletion)
                         and receipt.outcome in (TurnOutcome.DONE, TurnOutcome.LATE_DONE)
                         or isinstance(receipt, worker.DispatchWaitingForRuns)
                         and isinstance(receipt.reply, worker.DispatchComplete))
        if isinstance(event.native_stop, dict) and marker != stage and archived_stops:
            late = _recover_replaced_stops(event, row, run, instance, marker, archived_stops)
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
                receipt = _stopped_receipt(instance, delivery)
                notice_handled_here = True
                retry_stopped_diagnostics = True
                decision = _stopped_notice(row, parent, run, passed, stopped)
                if event.kind == EventKind.RUN_FINISHED or (
                        event.kind == EventKind.TURN_END and marker in ("", receipt.completion_id)):
                    recovered = _record_stopped_reply(row, run, receipt, stopped)
                    if isinstance(recovered, progress_history.HookCompletion):
                        retry_stopped_diagnostics = False
                    latest = recovered if isinstance(recovered, progress_history.HookCompletion) else stopped
                    return _close_stopped_after_notice(row, parent, run, instance, latest, decision)
                return decision
            return _ignored(event, "the recorder has not ended the stopped pass", row.name)
        client = herdr.Herdr(herdr.shared_session())
        assert state is not None and receipt is not None
        if isinstance(receipt, TurnOutcome):
            return receipt
        if isinstance(receipt, progress_history.HookCompletion):
            if receipt.finished:
                if needs_verification:
                    pending_path.unlink(missing_ok=True)
                if (receipt.pane_closed and receipt.outcome in (TurnOutcome.DONE, TurnOutcome.LATE_DONE)
                        and detached_runs.acknowledge_worker_completion(run, row, passed)
                        == detached_runs.CompletionReplay.RETRY):
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
            worker.save_reply(path, reply)
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

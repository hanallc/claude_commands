#!/usr/bin/env python3
"""Select staff workers and dispatch work through their native conversations."""
from __future__ import annotations

try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from scripts.direct import pass_record as pass_recorder

import argparse
from collections.abc import Callable, Generator, Iterable
from contextlib import ExitStack, contextmanager, redirect_stderr, suppress
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, replace
from enum import Enum, IntEnum
import fcntl
from importlib import import_module
from functools import cache, cached_property, partial
import hashlib
import io
import json
import math
import os
import re
import selectors
import shlex
import stat
import threading
import time
from pathlib import Path
import subprocess
import sys
import uuid
from typing import Literal, Protocol, TextIO, cast, final

from scripts.agents import detached_runs, pane_record, shutdown_record, worker_lifecycle
from scripts.herdr import herdr_types, pane_launch
from scripts.accounts import accounts, session_state
from scripts.direct import progress_history, recorder_state
from scripts.herdr import worker_panes
from scripts.herdr.worker_panes import (
    NoTranscriptTurn as NoTranscriptTurn, TranscriptCursor as TranscriptCursor, transcript_stream as transcript_stream,
)
from scripts.usage import rollouts
from scripts.lib import atomic_file, conf, file_lock, models_registry, unit_runs
from scripts.lib.transcripts import open_transcript
from scripts.lib.resume_message import BusyPass, IdleWorker, NoOwnPass, NO_OWN_PASS, PaneWorkerResume, ResumeWorkers, Unavailable, launcher_prompt, resume_message
from scripts.lib.resume_message import UnsubmittedAssignment as UnsubmittedAssignment
from scripts.lib.script_envelope import SCRIPT_ENVELOPE_END, SCRIPT_ENVELOPE_START, script_prompt
from scripts.production import display_cells, session_staff, staff, worker_admission
from scripts.production.production_doc import EnrolledPaths, ProductionPathsError
from scripts.whoami import quota_state

from scripts.herdr import herdr

StaffId = staff.StaffId
WorkerName = staff.WorkerName
WorkerRole = staff.WorkerRole
ExitCode = worker_panes.ExitCode
CompletionRecovery = worker_panes.CompletionRecovery


@dataclass(frozen=True)
class TranscriptTurn:
    """Identify the latest reply after the dispatch prompt."""

    cursor: TranscriptCursor
    reply: str
    delivered: TranscriptCursor | NoTranscriptTurn = NoTranscriptTurn.UNRECORDED


@dataclass(frozen=True)
class NoticePositions:
    """Keep the accepted and delivered positions for the current pass."""

    latest: TranscriptCursor | NoTranscriptTurn = NoTranscriptTurn.UNRECORDED
    delivered: TranscriptCursor | NoTranscriptTurn = NoTranscriptTurn.UNRECORDED

    def checkpoints(self, stream: str) -> tuple[TranscriptCursor, ...]:
        return tuple(replace(cursor, stream=stream) for cursor in (self.latest, self.delivered)
                     if isinstance(cursor, TranscriptCursor))

    def order(self, turn: TranscriptTurn, matched: frozenset[TranscriptCursor]) -> TranscriptTurn | NoTranscriptTurn:
        stream = turn.cursor.stream
        latest = self._confirmed(self.latest, stream, matched)
        delivered = self._confirmed(self.delivered, stream, matched)
        for saved in (self.latest, self.delivered):
            if isinstance(saved, TranscriptCursor) and saved.stream == stream and saved not in matched:
                return NoTranscriptTurn.UNRECORDED
        for boundary in (latest, delivered):
            if isinstance(boundary, TranscriptCursor) and not turn.cursor.at_or_after(boundary):
                return NoTranscriptTurn.UNRECORDED
        return replace(turn, delivered=delivered)

    @staticmethod
    def _confirmed(cursor: TranscriptCursor | NoTranscriptTurn, stream: str,
                   matched: frozenset[TranscriptCursor]) -> TranscriptCursor | NoTranscriptTurn:
        if isinstance(cursor, TranscriptCursor):
            checkpoint = replace(cursor, stream=stream)
            if checkpoint in matched:
                return checkpoint
        return NoTranscriptTurn.UNRECORDED


NO_NOTICE_POSITIONS = NoticePositions()
NO_TRANSCRIPT_CURSORS: frozenset[TranscriptCursor] = frozenset()


@dataclass(frozen=True)
class DispatchNotSubmitted:
    """The complete transcript contains no prompt for this dispatch."""


@dataclass(frozen=True)
class DispatchSubmitted:
    """The transcript contains the prompt without a matching final reply."""

    reply: str = ""
    compacted_after_prompt: bool = False
    turn: TranscriptTurn | NoTranscriptTurn = field(default=NoTranscriptTurn.UNRECORDED, compare=False)


@dataclass(frozen=True)
class TranscriptInputMessage:
    cursor: TranscriptCursor
    text: str
    digest: str


@dataclass(frozen=True)
class TranscriptResultReply:
    """A result message and its reply in the same transcript turn."""

    run_id: str
    message: TranscriptInputMessage
    reply: TranscriptTurn


@dataclass(frozen=True)
class ResultDeliveryProof:
    """A confirmed result permits recovery at the latest transcript reply."""

    done: TranscriptCursor
    delivery: TranscriptResultReply
    latest_reply: TranscriptTurn


class NoResultDeliveryProof(Enum):
    UNVERIFIED = "no result delivery proves recovery of the DONE"


@dataclass(frozen=True)
class DispatchComplete:
    reply: str
    anomalies: tuple[str, ...] = ()
    result_delivery: ResultDeliveryProof | NoResultDeliveryProof = field(
        default=NoResultDeliveryProof.UNVERIFIED, compare=False)


@dataclass(frozen=True)
class DispatchEvidenceUnknown:
    reason: str


@dataclass(frozen=True)
class DispatchWaitingForRuns:
    runs: tuple[str, ...]
    has_live_run: bool
    reply: DispatchSubmitted | DispatchComplete | DispatchAwaitingReply
    can_park: bool = False
    reap_required: frozenset[str] = frozenset()

    @property
    def waits_only_for_reap(self) -> bool:
        return bool(self.reap_required) and self.reap_required.issuperset(self.runs)


class NoMatchingDone(Enum):
    UNRECORDED = "the transcript has no matching DONE for this stage"


@dataclass(frozen=True)
class DispatchAwaitingReply:
    """A detached result requires a new matching DONE."""

    last_reply: str
    runs: tuple[str, ...]
    matching_done: DispatchComplete | NoMatchingDone


DispatchReceipt = (DispatchNotSubmitted | DispatchSubmitted | DispatchComplete | DispatchEvidenceUnknown
                   | DispatchWaitingForRuns | DispatchAwaitingReply)


@dataclass(frozen=True)
class ReplyBesidePrompt:
    """Write the reply beside the prompt when no destination is supplied."""


@dataclass(frozen=True)
class SingleStageReview:
    """Complete the dispatch without a second review stage."""


REPLY_BESIDE_PROMPT = ReplyBesidePrompt()
SINGLE_STAGE_REVIEW = SingleStageReview()


@dataclass(frozen=True)
class DoneReply:
    dispatch_id: str


@dataclass(frozen=True)
class FinalReply:
    """Accept the final message after the exact question."""


class DoneTimeUnknown(Enum):
    UNRECORDED = "the transcript does not record a DONE time"


@dataclass(frozen=True)
class TranscriptReceipt:
    receipt: DispatchNotSubmitted | DispatchSubmitted | DispatchComplete | DispatchEvidenceUnknown | DispatchAwaitingReply
    received_runs: frozenset[str]
    done_at: float | DoneTimeUnknown = DoneTimeUnknown.UNRECORDED
    cursor: TranscriptCursor | NoMatchingDone = NoMatchingDone.UNRECORDED
    results_before_done: frozenset[str] = frozenset()
    extends_saved: bool = False
    unreadable_pending_runs: tuple[str, ...] = ()
    result_replies: tuple[TranscriptResultReply, ...] = ()
    latest_reply: TranscriptTurn | NoTranscriptTurn = NoTranscriptTurn.UNRECORDED


@dataclass(frozen=True)
class DispatchPrompt:
    """Find the saved dispatch prompt without rebuilding its command or path."""

    dispatch_id: str
    include_stage_two: bool = False


def _prompt_names_completion(text: str, completion_id: str) -> bool:
    return (f"End your final reply with DONE {completion_id}." in text
            or f"End your final reply with this line alone: DONE {completion_id}\n" in text)


NO_RESULT_IDS: frozenset[str] = frozenset()


@final
class _TranscriptReceiptState:
    def __init__(self, line: str | DispatchPrompt, completion: DoneReply | FinalReply, *,
                 accept_late_done: bool, late_result_ids: frozenset[str], other_pass_results: frozenset[str]) -> None:
        self.line = line
        self.completion = completion
        self.accept_late_done = accept_late_done
        self.late_result_ids = late_result_ids
        self.other_pass_results = other_pass_results
        self.submitted = False
        self.responding = False
        self.turn_has_reply = False
        self.incomplete = False
        self.done_at: float | DoneTimeUnknown = DoneTimeUnknown.UNRECORDED
        self.received: set[str] = set()
        self.cursor: TranscriptCursor | NoMatchingDone = NoMatchingDone.UNRECORDED
        self.results_before_done: frozenset[str] = frozenset()
        self.result_message: TranscriptInputMessage | NoTranscriptTurn = NoTranscriptTurn.UNRECORDED
        self.result_replies: list[TranscriptResultReply] = []
        self.latest_reply: TranscriptTurn | NoTranscriptTurn = NoTranscriptTurn.UNRECORDED
        self.reply: DispatchSubmitted | DispatchComplete | DispatchAwaitingReply = DispatchSubmitted()
        self.completion_id = completion.dispatch_id if isinstance(completion, DoneReply) else ""
        self.second_stage = (worker_panes.stage_two_completion_id(line.dispatch_id)
                             if isinstance(line, DispatchPrompt) and line.include_stage_two else "")

    def user_message(self, text: str, cursor: TranscriptCursor | NoMatchingDone) -> None:
        self.turn_has_reply = False
        input_text = text
        text = text.strip()
        results = {part.removeprefix("Detached run: ") for part in text.splitlines()
                   if part.startswith("Detached run: ")}
        self.received.update(results.difference(self.other_pass_results))
        self.latest_reply = NoTranscriptTurn.UNRECORDED
        self.result_message = NoTranscriptTurn.UNRECORDED
        content = text
        if text.startswith(SCRIPT_ENVELOPE_START) and text.endswith(SCRIPT_ENVELOPE_END):
            content = text.removeprefix(SCRIPT_ENVELOPE_START).removesuffix(SCRIPT_ENVELOPE_END).strip()
        first, _, body = content.partition("\n")
        is_result = first.startswith("Detached run: ")
        assignment = (not is_result and "Read the unchanged prompt file" in text and any(instruction in text for instruction in (
            "End your final reply with DONE ", "End your final reply with this line alone: DONE ")))
        if (not is_result and self.second_stage and self.completion_id != self.second_stage
                and _prompt_names_completion(text, self.second_stage)):
            self.completion_id = self.second_stage
            self.reply = DispatchSubmitted()
            self.cursor = NoMatchingDone.UNRECORDED
            self.results_before_done = frozenset()
        if not is_result and (text in (self.line.strip(), script_prompt(self.line).strip())
                              if isinstance(self.line, str) else _prompt_names_completion(text, self.completion_id)):
            self.result_replies.clear()
            self.submitted = True
            self.responding = True
            if isinstance(self.reply, DispatchSubmitted):
                self.reply = DispatchSubmitted()
        elif isinstance(self.completion, FinalReply):
            self.responding = False
            self.reply = DispatchSubmitted()
        elif assignment:
            self.responding = False
            self.result_replies.clear()
            if isinstance(self.reply, DispatchSubmitted):
                self.reply = replace(self.reply, turn=NoTranscriptTurn.UNRECORDED)
        elif self.submitted:
            if (self.responding and isinstance(self.cursor, TranscriptCursor) and isinstance(cursor, TranscriptCursor)
                    and is_result and body.strip()):
                self.result_message = TranscriptInputMessage(cursor, content, worker_panes.worker_input_digest(input_text))
            results.difference_update(self.other_pass_results)
            if not self.responding:
                results.intersection_update(self.late_result_ids)
            if not results:
                return
            if isinstance(self.reply, DispatchAwaitingReply):
                self.reply = replace(self.reply, runs=tuple(sorted(results.union(self.reply.runs))))
            else:
                matching_done = self.reply if isinstance(self.reply, DispatchComplete) else NoMatchingDone.UNRECORDED
                self.reply = DispatchAwaitingReply(self.reply.reply, tuple(sorted(results)), matching_done)

    def assistant_message(self, text: str, timestamp: object,
                          cursor: TranscriptCursor | NoMatchingDone) -> None:
        self.turn_has_reply = True
        if not (self.responding or isinstance(self.line, DispatchPrompt) or self.accept_late_done and self.submitted):
            return
        if isinstance(self.completion, FinalReply):
            complete = bool(text.strip())
        else:
            if self.second_stage and worker_admission.reply_ends_dispatch(text, self.second_stage):
                self.completion_id = self.second_stage
            complete = worker_admission.reply_ends_dispatch(text, self.completion_id)
        if self.accept_late_done and not self.responding and not complete:
            return
        if complete and isinstance(self.line, DispatchPrompt):
            self.submitted = True
            self.responding = True
        if complete:
            self.latest_reply = NoTranscriptTurn.UNRECORDED
            self.result_replies.clear()
            self.result_message = NoTranscriptTurn.UNRECORDED
            self.reply = DispatchComplete(text)
            try:
                at = rollouts.parse_timestamp(timestamp)
                self.done_at = at.timestamp() if at is not None else DoneTimeUnknown.UNRECORDED
            except OverflowError:
                self.done_at = DoneTimeUnknown.UNRECORDED
            self.cursor = cursor
            self.results_before_done = frozenset(self.received)
            self.incomplete = False
            return
        if isinstance(cursor, TranscriptCursor):
            self.latest_reply = TranscriptTurn(cursor, text)
            if isinstance(self.result_message, TranscriptInputMessage):
                run_id = self.result_message.text.partition("\n")[0].removeprefix("Detached run: ")
                self.result_replies.append(TranscriptResultReply(run_id, self.result_message, self.latest_reply))
        if isinstance(self.reply, DispatchAwaitingReply):
            self.reply = replace(self.reply, last_reply=text)
        elif isinstance(self.completion, FinalReply) or isinstance(self.reply, DispatchSubmitted):
            turn = (TranscriptTurn(cursor, text) if self.responding and isinstance(cursor, TranscriptCursor)
                    else NoTranscriptTurn.UNRECORDED)
            self.reply = DispatchSubmitted(text, turn=turn)

    def turn_ended(self, cursor: TranscriptCursor | NoMatchingDone) -> None:
        if (not self.turn_has_reply and self.submitted and self.responding
                and isinstance(self.reply, DispatchSubmitted) and isinstance(cursor, TranscriptCursor)):
            self.reply = replace(self.reply, turn=TranscriptTurn(cursor, ""))
        self.turn_has_reply = False
        self.result_message = NoTranscriptTurn.UNRECORDED

    def compacted(self) -> None:
        if self.submitted and self.responding and isinstance(self.reply, DispatchSubmitted):
            self.reply = replace(self.reply, compacted_after_prompt=True)

    def finish(self, failure: str, *, extends_saved: bool = False,
               notice_positions: NoticePositions = NO_NOTICE_POSITIONS,
               matched_cursors: frozenset[TranscriptCursor] = NO_TRANSCRIPT_CURSORS) -> TranscriptReceipt:
        if not failure and self.incomplete:
            failure = "worker transcript has incomplete records"
        if failure:
            pending = self.reply.runs if isinstance(self.reply, DispatchAwaitingReply) else ()
            return TranscriptReceipt(DispatchEvidenceUnknown(failure), frozenset(self.received),
                                     extends_saved=extends_saved, unreadable_pending_runs=pending)
        reply = self.reply if self.submitted else DispatchNotSubmitted()
        if isinstance(reply, DispatchSubmitted) and isinstance(reply.turn, TranscriptTurn):
            reply = replace(reply, turn=notice_positions.order(reply.turn, matched_cursors))
        return TranscriptReceipt(reply, frozenset(self.received), self.done_at, self.cursor,
                                 self.results_before_done, extends_saved, result_replies=tuple(self.result_replies),
                                 latest_reply=self.latest_reply)


def _read_transcript(entries: Iterable[str | dict[str, object] | worker_admission.TornTranscriptRecord],
                     *states: _TranscriptReceiptState, stream: str = "",
                     previous: tuple[TranscriptCursor, ...] = (),
                     ) -> tuple[str, frozenset[TranscriptCursor]]:
    checkpoints = set(previous)
    matched: set[TranscriptCursor] = set()
    try:
        records = (worker_panes.transcript_record_cursors(entries, stream) if stream else
                   ((entry, NoMatchingDone.UNRECORDED) for entry in entries))
        for entry, cursor in records:
            if isinstance(cursor, TranscriptCursor) and cursor in checkpoints:
                matched.add(cursor)
            if isinstance(entry, worker_admission.TornTranscriptRecord):
                for state in states:
                    state.incomplete = True
                continue
            timestamp = None
            if isinstance(entry, dict):
                fields = entry
                timestamp = fields.get("timestamp")
                entry = worker_admission.transcript_entry(fields, set())
                if fields.get("isMeta") is not True:
                    if rollouts.is_compaction(cast(rollouts.RolloutRecordJson, cast(object, fields))):
                        for state in states:
                            state.compacted()
                        continue
                    if not entry and isinstance(cursor, TranscriptCursor) and _transcript_turn_boundary(fields):
                        for state in states:
                            state.turn_ended(cursor)
            if not entry.startswith(("user:\n", "assistant:\n")):
                continue
            if entry.startswith("user:\n"):
                message = entry.removeprefix("user:\n")
                for state in states:
                    state.user_message(message, cursor)
            else:
                message = entry.removeprefix("assistant:\n")
                for state in states:
                    state.assistant_message(message, timestamp, cursor)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return str(error) or type(error).__name__, frozenset(matched)
    return "", frozenset(matched)


def _transcript_turn_boundary(fields: dict[str, object]) -> bool:
    if fields.get("type") == "system" and fields.get("subtype") == "turn_duration":
        return True
    if fields.get("type") == "event_msg":
        payload = fields.get("payload")
        return isinstance(payload, dict) and cast(dict[str, object], payload).get("type") == "task_complete"
    return False


def transcript_receipt(entries: Iterable[str | dict[str, object] | worker_admission.TornTranscriptRecord],
                       line: str | DispatchPrompt, completion: DoneReply | FinalReply, *,
                       accept_late_done: bool = False,
                       late_result_ids: frozenset[str] = NO_RESULT_IDS,
                       other_pass_results: frozenset[str] = NO_RESULT_IDS) -> TranscriptReceipt:
    """Read prompt acceptance and its reply through the shared transcript parser."""
    state = _TranscriptReceiptState(line, completion, accept_late_done=accept_late_done,
                                    late_result_ids=late_result_ids, other_pass_results=other_pass_results)
    failure, _ = _read_transcript(entries, state)
    return state.finish(failure)


def consultation_reply(client: herdr.Herdr, pane: str, source: Path, question: str) -> str:
    """Require acceptance before the turn end and its final reply."""
    deadline = time.monotonic() + herdr.PROGRAM_TIMEOUT_SECONDS
    receipt = transcript_receipt(worker_admission.transcript_entries(source, set()), question, FinalReply()).receipt
    while True:
        accepted = isinstance(receipt, (DispatchSubmitted, DispatchComplete))
        if not accepted and time.monotonic() >= deadline:
            raise ValueError("question was not accepted")
        ended = client.wait_turn_end(pane, int(herdr.PROGRAM_TIMEOUT_SECONDS * 1000))
        if not accepted:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(0.1, remaining))
        receipt = transcript_receipt(worker_admission.transcript_entries(source, set()), question, FinalReply()).receipt
        if isinstance(receipt, DispatchEvidenceUnknown):
            raise ValueError(receipt.reason)
        if accepted and isinstance(ended, herdr.ObservedAgentState):
            if isinstance(receipt, DispatchComplete):
                return receipt.reply
            raise ValueError("no review was written")


class CurrentPanePass(Enum):
    CURRENT = "read the current pane pass"


def _dispatch_transcript(
    row: staff.StaffRow, line: str, done: DoneReply, *, require_transcript: bool,
    previous: TranscriptCursor | NoMatchingDone = NoMatchingDone.UNRECORDED,
    notice_positions: NoticePositions = NO_NOTICE_POSITIONS,
    stopped: bool = False, late_result_ids: frozenset[str] = NO_RESULT_IDS,
    other_pass_results: frozenset[str] = NO_RESULT_IDS,
    exact_prompt: bool = False,
) -> TranscriptReceipt:
    backend = staff.backend(row)
    if isinstance(backend, staff.BackendUnavailable):
        return TranscriptReceipt(DispatchEvidenceUnknown("worker transcript backend is unavailable"), NO_RESULT_IDS)
    try:
        source = worker_admission.transcript_source(row, backend)
        stream = transcript_stream(row, source)
    except (OSError, ValueError) as error:
        if not require_transcript and isinstance(backend, staff.ClaudeBackend) and str(error) == "no source transcript found":
            return TranscriptReceipt(DispatchNotSubmitted(), NO_RESULT_IDS)
        return TranscriptReceipt(DispatchEvidenceUnknown(str(error)), NO_RESULT_IDS)

    exact = _TranscriptReceiptState(line, done, accept_late_done=stopped, late_result_ids=late_result_ids,
                                    other_pass_results=other_pass_results)
    fallback = _TranscriptReceiptState(DispatchPrompt(done.dispatch_id), done,
                                       accept_late_done=stopped, late_result_ids=late_result_ids,
                                       other_pass_results=other_pass_results)
    checkpoints = notice_positions.checkpoints(stream)
    if isinstance(previous, TranscriptCursor):
        checkpoints += (previous,)
    failure, matched = _read_transcript(worker_admission.transcript_records(source), exact, fallback,
                                        stream=stream, previous=checkpoints)
    receipt = exact.finish(failure, extends_saved=previous in matched,
                           notice_positions=notice_positions, matched_cursors=matched)
    if not exact_prompt and isinstance(receipt.receipt, DispatchNotSubmitted):
        return fallback.finish(failure, extends_saved=previous in matched,
                               notice_positions=notice_positions, matched_cursors=matched)
    return receipt


def _done_covers_finish(done_at: float | DoneTimeUnknown,
                        finished_at: float | detached_runs.RunFinishedBefore | detached_runs.RunFinishTimeUnknown,
                        ) -> bool:
    if not isinstance(done_at, float):
        return False
    if isinstance(finished_at, detached_runs.RunFinishedBefore):
        return finished_at.exclusive_bound <= done_at
    return isinstance(finished_at, float) and finished_at < done_at


def _done_covers_run(done_at: float | DoneTimeUnknown, record: detached_runs.RunRecord,
                     finished_at: float | detached_runs.RunFinishedBefore | detached_runs.RunFinishTimeUnknown) -> bool:
    if _done_covers_finish(done_at, finished_at):
        return True
    link = record.get("pane_dispatch_id")
    return (isinstance(done_at, float) and isinstance(link, str) and bool(link)
            and isinstance(finished_at, (float, detached_runs.RunFinishedBefore))
            and record["started_at"] < done_at and not record.get("stop_pass_requested")
            and not record.get("self_stop"))


@dataclass(frozen=True)
class VerifyPassDone:
    """Save transcript evidence while the caller holds the worker lock."""

    instance: str
    notice_positions: NoticePositions = NO_NOTICE_POSITIONS


class ReadPassDone(Enum):
    READ = "read without saving reply evidence"


@dataclass(frozen=True)
class VerifiedPassDone:
    """Supply the verified occurrence and its run coverage."""

    reply: DispatchComplete
    cursor: TranscriptCursor
    done_at: float | DoneTimeUnknown
    results_before_done: frozenset[str]

    def covers_run(self, record: detached_runs.RunRecord,
                   finished_at: float | detached_runs.RunFinishedBefore | detached_runs.RunFinishTimeUnknown,
                   ) -> detached_runs.ResultDoneCoverage | detached_runs.ResultTranscriptUnknown:
        if record["status"] == "finished" and record["run_id"] in self.results_before_done:
            return detached_runs.ResultDoneCoverage.COVERED
        if finished_at is detached_runs.RunFinishTimeUnknown.UNRECORDED:
            return detached_runs.ResultTranscriptUnknown(finished_at.value)
        if _done_covers_run(self.done_at, record, finished_at):
            return detached_runs.ResultDoneCoverage.COVERED
        return detached_runs.ResultDoneCoverage.NEEDS_DELIVERY


@dataclass(frozen=True)
class SavedPassDone:
    done: VerifiedPassDone
    received_runs: frozenset[str]
    pending_runs: frozenset[str]


def pending_done_path(run: Path, worker_id: StaffId) -> Path:
    return run / f".{worker_id}.pending-done.json"


def _pass_done_path(run: Path, row: staff.StaffRow, stage: str) -> Path:
    digest = hashlib.sha256(f"{row.id}:{stage}".encode()).hexdigest()
    return run / f".{digest}.pass-done.json"


def _read_pass_done(path: Path, instance: object, stage: str) -> SavedPassDone | NoMatchingDone:
    saved = worker_lifecycle.read_json_object(path)
    if not saved or saved.get("instance") != instance:
        return NoMatchingDone.UNRECORDED
    raw_cursor = saved.get("cursor")
    if raw_cursor is None:
        return NoMatchingDone.UNRECORDED
    cursor = TranscriptCursor.from_json(raw_cursor)
    if not isinstance(cursor, TranscriptCursor):
        raise ValueError(f"{path}: invalid DONE cursor")
    reply = saved.get("reply")
    if not isinstance(reply, str) or not worker_admission.reply_ends_dispatch(reply, stage):
        raise ValueError(f"{path}: invalid saved DONE reply")

    def run_ids(key: str) -> frozenset[str]:
        value = saved.get(key)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in cast(list[object], value)):
            raise ValueError(f"{path}: invalid {key}")
        return frozenset(cast(list[str], value))

    at = saved.get("transcript_done_at")
    done = VerifiedPassDone(DispatchComplete(reply), cursor,
                            float(at) if isinstance(at, (int, float)) else DoneTimeUnknown.UNRECORDED,
                            run_ids("results_before_done"))
    return SavedPassDone(done, run_ids("received_runs"), run_ids("pending_runs"))


def _save_pass_done(path: Path, instance: str, saved: SavedPassDone) -> None:
    done = saved.done
    fields = {
        "instance": instance, "reply": done.reply.reply, "cursor": asdict(done.cursor),
        "transcript_done_at": done.done_at if isinstance(done.done_at, float) else None,
        "received_runs": sorted(saved.received_runs), "pending_runs": sorted(saved.pending_runs),
        "results_before_done": sorted(done.results_before_done),
    }
    atomic_file.write_text_atomic(path, json.dumps(fields))


def _select_pass_done(transcript: TranscriptReceipt, saved: SavedPassDone | NoMatchingDone,
                      ) -> VerifiedPassDone | NoMatchingDone:
    reply = transcript.receipt
    matching = reply.matching_done if isinstance(reply, DispatchAwaitingReply) else reply
    cursor = transcript.cursor
    if isinstance(saved, SavedPassDone) and (
            not transcript.extends_saved or not isinstance(cursor, TranscriptCursor)
            or cursor.position <= saved.done.cursor.position):
        return saved.done
    if isinstance(matching, DispatchComplete) and isinstance(cursor, TranscriptCursor):
        return VerifiedPassDone(matching, cursor, transcript.done_at, transcript.results_before_done)
    return NoMatchingDone.UNRECORDED


def read_result_done_evidence(
    run: Path, row: staff.StaffRow, passed: dict[str, object],
) -> VerifiedPassDone | detached_runs.ResultTranscriptUnknown | Literal[detached_runs.ResultDoneCoverage.NEEDS_DELIVERY]:
    """Select DONE evidence for the current pass before checking its results."""
    delivery = pass_recorder.dispatch_delivery(passed)
    if not delivery.line or not delivery.completion_id:
        return detached_runs.ResultDoneCoverage.NEEDS_DELIVERY
    saved = _read_pass_done(_pass_done_path(run, row, delivery.completion_id), passed.get("instance_id"),
                            delivery.completion_id)
    transcript = _dispatch_transcript(row, delivery.line, DoneReply(delivery.completion_id), require_transcript=True,
        previous=saved.done.cursor if isinstance(saved, SavedPassDone) else NoMatchingDone.UNRECORDED)
    verified = _select_pass_done(transcript, saved)
    if isinstance(verified, VerifiedPassDone):
        return verified
    if isinstance(transcript.receipt, DispatchEvidenceUnknown):
        return detached_runs.ResultTranscriptUnknown(transcript.receipt.reason)
    return detached_runs.ResultDoneCoverage.NEEDS_DELIVERY


def result_done_coverage(row: staff.StaffRow, passed: dict[str, object], record: detached_runs.RunRecord,
                         ) -> detached_runs.ResultDoneCoverage | detached_runs.ResultTranscriptUnknown:
    """Check the saved prompt and DONE for a result that belongs to this pass."""
    verified = read_result_done_evidence(Path(record["session_dir"]), row, passed)
    if isinstance(verified, VerifiedPassDone):
        return verified.covers_run(record, detached_runs.run_finish_time(record))
    if verified is detached_runs.ResultDoneCoverage.NEEDS_DELIVERY:
        return detached_runs.ResultDoneCoverage.NO_MATCHING_DONE
    return verified


def _prove_result_delivery(directory: Path, row: staff.StaffRow, passed: dict[str, object],
                           transcript: TranscriptReceipt, done: VerifiedPassDone
                           ) -> ResultDeliveryProof | NoResultDeliveryProof:
    latest = transcript.latest_reply
    if done.cursor != transcript.cursor or not isinstance(latest, TranscriptTurn) or not transcript.result_replies:
        return NoResultDeliveryProof.UNVERIFIED

    candidates = transcript.result_replies
    if candidates[-1].reply.cursor == latest.cursor:
        candidates = candidates[-1:]
    current = detached_runs.result_submission_pass(passed)
    started = passed.get("started_at")
    records = {record["run_id"]: record for record in detached_runs.confirmed_worker_results(
        directory, row, {candidate.run_id for candidate in candidates})}
    for candidate in reversed(candidates):
        record = records.get(candidate.run_id)
        if record is None:
            continue
        receipt = detached_runs.read_worker_result_receipt(record)
        if (not isinstance(receipt, detached_runs.WorkerResultReceipt)
                or not isinstance(receipt.assignment, detached_runs.ResultSubmissionPass)
                or not receipt.identifies(record, row, directory.parent)
                or receipt.cursor != candidate.message.cursor or receipt.input_digest != candidate.message.digest
                or not _done_covers_run(done.done_at, record, detached_runs.run_finish_time(record))):
            continue
        if receipt.assignment == current and candidate.reply.cursor == latest.cursor:
            return ResultDeliveryProof(done.cursor, candidate, latest)
        if (isinstance(current, detached_runs.ResultSubmissionPass)
                and receipt.assignment.instance != current.instance
                and isinstance(started, (int, float)) and record["started_at"] < started):
            return ResultDeliveryProof(done.cursor, candidate, latest)
    return NoResultDeliveryProof.UNVERIFIED


def dispatch_receipt(row: staff.StaffRow, line: str, dispatch_id: str, *,
                     run: Path, require_transcript: bool = False,
                     pane_pass: CurrentPanePass | progress_history.StoppedPassReceipt | worker_panes.PassDetachedResults = CurrentPanePass.CURRENT,
                     require_done: bool = False, verify: VerifyPassDone | ReadPassDone = ReadPassDone.READ,
                     ) -> DispatchReceipt:
    """Check reply evidence and the command exits required by the pass."""
    directory = run / worker_lifecycle.DETACHED_RUNS_DIR
    done = DoneReply(dispatch_id)
    stopped = isinstance(pane_pass, progress_history.StoppedPassReceipt)
    done_path = _pass_done_path(run, row, dispatch_id)
    passed: dict[str, object] = {}
    saved: SavedPassDone | NoMatchingDone = NoMatchingDone.UNRECORDED
    if done_path.is_file():
        if stopped:
            saved = _read_pass_done(done_path, pane_pass.instance_id, dispatch_id)
        else:
            passed = worker_panes.worker_pass(row, run)
            if pass_recorder.dispatch_delivery(passed).completion_id == dispatch_id:
                saved = _read_pass_done(done_path, passed.get("instance_id"), dispatch_id)
    late_result_ids = other_pass_results = NO_RESULT_IDS
    if stopped:
        legacy = pane_pass.results
        include_legacy = isinstance(legacy, progress_history.LegacyResultsForStoppedPass)
        since = legacy.pass_started_at if include_legacy else 0
        deliveries, other_passes = detached_runs.worker_results(directory, row, since=since,
            pane_dispatch_id=pane_pass.instance_id, include_legacy_pass=include_legacy)
        late_result_ids = frozenset(deliveries)
        other_pass_results = frozenset(other_passes)
    else:
        deliveries = {}

    previous = saved.done.cursor if isinstance(saved, SavedPassDone) else NoMatchingDone.UNRECORDED
    notice_positions = verify.notice_positions if isinstance(verify, VerifyPassDone) else NO_NOTICE_POSITIONS
    transcript = _dispatch_transcript(row, line, done, require_transcript=require_transcript, previous=previous,
        notice_positions=notice_positions, stopped=stopped, late_result_ids=late_result_ids,
        other_pass_results=other_pass_results)
    verified = _select_pass_done(transcript, saved)
    reply = transcript.receipt
    if not isinstance(verified, VerifiedPassDone):
        if isinstance(reply, (DispatchNotSubmitted, DispatchEvidenceUnknown)):
            return reply
        if require_done and isinstance(reply, DispatchSubmitted):
            return reply
    live = ([] if isinstance(pane_pass, worker_panes.PassDetachedResults) and pane_pass.live_runs_checked else
            detached_runs.live_runs(directory, row.name, identity=row.id))
    if isinstance(pane_pass, progress_history.StoppedPassReceipt):
        instance = pane_pass.instance_id
        if isinstance(pane_pass.results, progress_history.LegacyResultsForStoppedPass):
            live = [record for record in live if not detached_runs.result_is_from_another_pass(record, instance)]
        else:
            live = [record for record in live if record.get("pane_dispatch_id") == instance]
    else:
        if isinstance(pane_pass, worker_panes.PassDetachedResults):
            since, instance = pane_pass.started_at, pane_pass.instance_id
        else:
            if not passed:
                passed = worker_panes.worker_pass(row, run)
            started = passed.get("started_at", 0)
            since = float(started) if isinstance(started, (int, float)) else 0
            instance = passed.get("instance_id")
        live = [record for record in live if not detached_runs.result_is_from_another_pass(record, instance)]
        deliveries, other_passes = detached_runs.worker_results(directory, row, since=since,
            pane_dispatch_id=instance if isinstance(instance, str) else "", include_legacy_pass=True)
        if isinstance(reply, DispatchAwaitingReply) and other_passes.intersection(reply.runs):
            transcript = _dispatch_transcript(row, line, done, require_transcript=require_transcript, previous=previous,
                notice_positions=notice_positions, other_pass_results=frozenset(other_passes))
            verified = _select_pass_done(transcript, saved)
            reply = transcript.receipt
        other_pass_results = frozenset(other_passes)
    read_extends_saved = not isinstance(saved, SavedPassDone) or transcript.extends_saved
    received = transcript.received_runs if read_extends_saved else NO_RESULT_IDS
    if isinstance(saved, SavedPassDone):
        received |= saved.received_runs
    if isinstance(verified, VerifiedPassDone):
        pending_runs: set[str] = set()
        if read_extends_saved:
            pending_runs.update(reply.runs if isinstance(reply, DispatchAwaitingReply)
                                else transcript.unreadable_pending_runs)
        if isinstance(saved, SavedPassDone):
            pending_runs.update(saved.pending_runs)
        pending_runs.difference_update(verified.results_before_done | other_pass_results)
        if pending_runs:
            last_reply = (reply.last_reply if read_extends_saved and isinstance(reply, DispatchAwaitingReply)
                          else verified.reply.reply)
            reply = DispatchAwaitingReply(last_reply, tuple(sorted(pending_runs)), verified.reply)
        else:
            reply = verified.reply
    if isinstance(reply, (DispatchNotSubmitted, DispatchEvidenceUnknown)):
        return reply
    covered_by_reply = {run_id for run_id, result in deliveries.items()
                        if isinstance(verified, VerifiedPassDone)
                        and verified.covers_run(result.run, result.finished_at) == detached_runs.ResultDoneCoverage.COVERED}
    covered = {run_id for run_id, result in deliveries.items()
               if result.delivered_at is detached_runs.WorkerResultCovered.BY_DONE} | covered_by_reply
    if isinstance(reply, DispatchAwaitingReply) and isinstance(reply.matching_done, DispatchComplete):
        later = tuple(run_id for run_id in reply.runs if run_id not in covered)
        reply = replace(reply, runs=later) if later else reply.matching_done
    if isinstance(verify, VerifyPassDone) and isinstance(verified, VerifiedPassDone):
        outstanding = frozenset(reply.runs) if isinstance(reply, DispatchAwaitingReply) else NO_RESULT_IDS
        evidence = SavedPassDone(verified, received, outstanding)
        if evidence != saved:
            _save_pass_done(done_path, verify.instance, evidence)
    reap_required = {run_id for run_id, result in deliveries.items()
                     if result.delivered_at is detached_runs.WorkerResultDeferred.REAP_REQUIRED}
    unseen = {run_id for run_id in deliveries.keys() - received - covered - reap_required
              if deliveries[run_id].delivered_at is not detached_runs.WorkerResultDeliverySuppressed.SUCCESS_ONLY}
    live_ids = {record["run_id"] for record in live}
    pending = unseen | live_ids | reap_required
    pending.update(run_id for run_id, result in deliveries.items()
                   if result.requires_done_after_finish and run_id not in covered)
    if isinstance(reply, DispatchAwaitingReply):
        pending.update(reply.runs)
    if unseen and isinstance(reply, DispatchComplete) and isinstance(verified, VerifiedPassDone):
        done_at = verified.done_at
        after_done = tuple(sorted(run_id for run_id in unseen if not (
            isinstance(done_at, float)
            and isinstance(delivered_at := deliveries[run_id].delivered_at, (int, float))
            and delivered_at < done_at)))
        if after_done:
            reply = DispatchAwaitingReply(reply.reply, after_done, reply)
        elif not live and not reap_required:
            reply = replace(reply, anomalies=tuple(
                f"run {run_id} finished, but its result is not in the transcript" for run_id in sorted(unseen)))
    if isinstance(reply, DispatchComplete) and reply.anomalies:
        return reply
    if pending:
        can_park = (not stopped and not reap_required and not isinstance(reply, DispatchAwaitingReply) and pending == live_ids
                    and isinstance(instance, str) and bool(instance)
                    and all(record.get("pane_dispatch_id") == instance and not record.get("stop_pass_requested")
                            and not record.get("self_stop") for record in live))
        return DispatchWaitingForRuns(tuple(sorted(pending)), bool(live), reply, can_park,
                                      reap_required=frozenset(reap_required))
    if isinstance(reply, DispatchComplete) and isinstance(verified, VerifiedPassDone):
        if (not stopped and isinstance(verify, VerifyPassDone) and verify.instance == instance
                and pass_recorder.dispatch_delivery(passed).completion_id == dispatch_id):
            reply = replace(reply, result_delivery=_prove_result_delivery(directory, row, passed, transcript, verified))
        finished_after_done = [
            deliveries[run_id].run for run_id in sorted(covered_by_reply)
            if run_id not in verified.results_before_done
            and not _done_covers_finish(verified.done_at, deliveries[run_id].finished_at)]
        if finished_after_done:
            body, _, done_line = reply.reply.rstrip().rpartition("\n")
            sections = [f"Detached run: {record['run_id']}\n"
                        + detached_runs.result_message(record, detached_runs.command_result(record))
                        for record in finished_after_done]
            text = "\n\n".join(part for part in (body.rstrip(), *sections, done_line) if part)
            return replace(reply, reply=text)
    return reply


def completion_close_evidence(row: staff.StaffRow, delivery: pass_recorder.DispatchDelivery, *,
                              run: Path, reply_file: str) -> worker_panes.CloseAllowed | worker_panes.Kept:
    receipt = dispatch_receipt(row, delivery.line, delivery.completion_id, run=run, require_transcript=True)
    return _saved_reply_evidence(receipt, reply_file)


def _saved_reply_evidence(receipt: DispatchReceipt, reply_file: str) -> worker_panes.CloseAllowed | worker_panes.Kept:
    if isinstance(receipt, DispatchComplete):
        if not reply_file:
            return worker_panes.Kept("the dispatch has no saved reply path")
        try:
            saved = Path(reply_file).read_text(encoding="utf-8")
        except OSError as error:
            return worker_panes.Kept(f"the dispatch reply is unavailable: {error}")
        if saved.rstrip() == receipt.reply.rstrip():
            return worker_panes.CloseAllowed()
        return worker_panes.Kept("the saved reply differs from the dispatch receipt")
    if isinstance(receipt, DispatchEvidenceUnknown):
        return worker_panes.Kept(f"completion evidence is unavailable: {receipt.reason}")
    return worker_panes.Kept("the transcript has no completed dispatch receipt")


def is_stage_one_review(delivery: pass_recorder.DispatchDelivery, instance: str) -> bool:
    return bool(delivery.early_ready) and delivery.completion_id == instance


def waiting_recovery(receipt: DispatchWaitingForRuns | DispatchAwaitingReply) -> CompletionRecovery:
    """Name what an unfinished receipt waits for: reap, a live run, or a new matching DONE."""
    if isinstance(receipt, DispatchAwaitingReply):
        return CompletionRecovery.WAITING_ON_REPLY
    if receipt.waits_only_for_reap:
        return CompletionRecovery.WAITING_ON_REAP
    return CompletionRecovery.WAITING_ON_RUNS if receipt.has_live_run else CompletionRecovery.WAITING_ON_REPLY


def pass_reply_evidence(row: staff.StaffRow, delivery: pass_recorder.DispatchDelivery, *,
                        run: Path, reply_file: str, require_completion: bool,
                        detached_results: worker_panes.PassDetachedResults) -> worker_panes.CloseAllowed | worker_panes.Kept:
    """Check one pass receipt for detached results and any required DONE."""
    receipt = dispatch_receipt(row, delivery.line, delivery.completion_id, run=run,
                               require_transcript=True, pane_pass=detached_results, require_done=require_completion)
    if isinstance(receipt, DispatchEvidenceUnknown):
        return worker_panes.Kept(f"completion evidence is unavailable: {receipt.reason}")
    if isinstance(receipt, (DispatchWaitingForRuns, DispatchAwaitingReply)):
        return worker_panes.Kept(waiting_recovery(receipt))
    if require_completion:
        if (detached_results.live_runs_checked and isinstance(receipt, DispatchComplete)
                and not is_stage_one_review(delivery, detached_results.instance_id)
                and reply_file and not os.path.lexists(reply_file)):
            # The assignment can save the proved reply under the worker lock.
            return worker_panes.CloseAllowed()
        if not isinstance(_saved_reply_evidence(receipt, reply_file), worker_panes.CloseAllowed):
            return worker_panes.Kept("submitted pass awaits completion")
    return worker_panes.CloseAllowed()


def _client() -> herdr.Herdr:
    return herdr.Herdr(herdr.shared_session())


def _agent_command(client: herdr.Herdr, worker: worker_panes.OpenWorker, action: str, line: str = "", *,
                   deadline: float = math.inf,
                   on_submission: Callable[[], object] = lambda: None) -> None:
    if not herdr.check_target(str(worker.row.current_session), action="keys",
            place=herdr.HerdrPlace(client.session, worker.pane), terminal=worker.terminal, text=line, client=client):
        raise herdr_types.HerdrFailure(f"{worker.row.name}: dispatch target is unproved", "wrong_target")
    arguments = ["agent", "prompt" if action == "compact" else action, worker.pane]
    if action == "prompt":
        arguments.append(script_prompt(line))
    elif action == "compact":
        arguments.extend(("/compact", "--timeout", str(max(0, int((deadline - time.monotonic()) * 1000))), "--wait"))
    command = client.argv(*arguments)
    _ = on_submission()
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    herdr.require_success(result, f"herdr agent {action}")


@dataclass(frozen=True)
class CompactionRequired:
    backend: staff.CodexBackend
    offset: int


class CompactionNotRequired(Enum):
    SKIP = "the conversation needs no compaction"


def _compaction_plan(row: staff.StaffRow) -> CompactionRequired | CompactionNotRequired:
    backend = staff.backend(row)
    if not isinstance(backend, staff.CodexBackend):
        return CompactionNotRequired.SKIP
    try:
        source = worker_admission.transcript_source(row, backend)
    except ValueError:
        return CompactionNotRequired.SKIP
    tokens = 0
    with open_transcript(source) as stream, io.TextIOWrapper(stream, encoding="utf-8") as transcript:
        for line in transcript:
            record = rollouts.decode_rollout(line)
            if record is None:
                continue
            if rollouts.is_compaction(record):
                tokens = 0
            usage = rollouts.token_usage(record)
            if usage is not None:
                tokens = usage.uncached_input + usage.cached_input + usage.output
        offset = stream.tell()
    return (CompactionRequired(backend, offset) if tokens > conf.read("direct").whole("unit_compact_above_tokens")
            else CompactionNotRequired.SKIP)


def _compact_before_dispatch(client: herdr.Herdr, current: worker_panes.OpenWorker) -> None:
    """Compact a large Codex conversation between assignments."""
    plan = _compaction_plan(current.row)
    if not isinstance(plan, CompactionRequired):
        return
    deadline = time.monotonic() + conf.read("direct").number("worker_compact_seconds")
    try:
        _agent_command(client, current, "compact", deadline=deadline)
        ended = client.wait_turn_end(current.pane, max(0, int((deadline - time.monotonic()) * 1000)))
        timed_out = isinstance(ended, herdr.WaitTimedOut)
    except herdr_types.HerdrFailure as error:
        if error.code != "timeout":
            raise
        timed_out = True
    source = worker_admission.transcript_source(current.row, plan.backend)
    with open_transcript(source) as stream:
        _ = stream.seek(plan.offset)
        for line in stream:
            if not line.endswith(b"\n"):
                break
            record = rollouts.decode_rollout(line.decode("utf-8"))
            if record is not None and rollouts.is_compaction(record):
                return
    if timed_out:
        raise herdr_types.HerdrFailure("compaction did not finish before the compaction bound", "timeout")
    raise herdr_types.HerdrFailure("compaction has no transcript proof", "not_submitted")


def _startup_failure(result: worker_panes.WorkerStart) -> tuple[ExitCode, str]:
    if isinstance(result, worker_panes.AdmissionRefused):
        return ExitCode.REFUSED, result.reason
    if isinstance(result, worker_panes.StartRefused):
        return ExitCode.REFUSED, f"{result.reason.value}: {result.detail}"
    if isinstance(result, worker_panes.IncompleteStart):
        return ExitCode.UNCERTAIN, f"incomplete startup: {result.reason}"
    if isinstance(result, worker_panes.UnpublishedWorker):
        return ExitCode.UNCERTAIN, f"publication failed: {result.cause}; {worker_panes.unpublished_cleanup_text(result)}"
    if isinstance(result, (worker_panes.ProcessUnknownWorker, worker_panes.UncertainWorker)):
        return ExitCode.UNCERTAIN, result.reason
    return ExitCode.OK, ""


def startup_result(name: WorkerName, result: worker_panes.WorkerStart) -> ExitCode:
    code, reason = _startup_failure(result)
    if isinstance(result, worker_panes.AdmissionRefused):
        print(f"staff: {name.value}: not opened: {reason}", file=sys.stderr)
    elif isinstance(result, worker_panes.StartRefused):
        print(reason, file=sys.stderr)
    elif isinstance(result, (worker_panes.IncompleteStart, worker_panes.UnpublishedWorker)):
        print(f"{name.value}: {reason}", file=sys.stderr)
    return code


@dataclass(frozen=True)
class ExistingArtifact:
    content: bytes
    mode: int


class ArtifactAbsent(Enum):
    MISSING = "the destination does not exist"


def _read_review_artifact(parent: int, path: Path) -> ExistingArtifact | ArtifactAbsent:
    try:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    except FileNotFoundError:
        return ArtifactAbsent.MISSING
    with os.fdopen(descriptor, "rb") as source:
        mode = os.fstat(source.fileno()).st_mode
        if not stat.S_ISREG(mode):
            raise OSError(f"{path}: expected a regular completion file")
        return ExistingArtifact(source.read(), stat.S_IMODE(mode))


def _write_review_artifact(parent: int, path: Path, content: bytes, *, create_only: bool = False) -> None:
    existing = _read_review_artifact(parent, path)
    if isinstance(existing, ExistingArtifact):
        if create_only:
            raise FileExistsError(f"{path}: the artifact already exists")
        if existing.content == content:
            return
    temporary = f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o666, dir_fd=parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            if isinstance(existing, ExistingArtifact):
                os.fchmod(output.fileno(), existing.mode)
            _ = output.write(content)
            output.flush()
            os.fsync(output.fileno())
        if isinstance(existing, ArtifactAbsent):
            os.link(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
        else:
            if _read_review_artifact(parent, path) != existing:
                raise OSError(f"{path}: completion file changed before publication")
            os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=parent)


def _read_ordinary_artifact(path: Path) -> ExistingArtifact | ArtifactAbsent:
    try:
        with path.open("rb") as source:
            return ExistingArtifact(source.read(), stat.S_IMODE(os.fstat(source.fileno()).st_mode))
    except FileNotFoundError:
        if path.is_symlink():
            raise
        return ArtifactAbsent.MISSING


def _write_ordinary_artifact(path: Path, content: bytes) -> None:
    existing = _read_ordinary_artifact(path)
    if isinstance(existing, ExistingArtifact) and existing.content == content:
        return
    mode = existing.mode if isinstance(existing, ExistingArtifact) else 0o600
    with atomic_file.prepared_replacement(path, content, mode=mode) as temporary:
        if _read_ordinary_artifact(path) != existing:
            raise OSError(f"{path}: completion file changed before publication")
        os.replace(temporary, path)
    atomic_file.sync_folder(path.parent)


def save_reply(path: Path, reply: str) -> None:
    """Save a plain reply through the ordinary path policy."""
    _write_ordinary_artifact(path, (reply.rstrip() + "\n").encode())


class ReviewRunAccess(Enum):
    RECORDED = "the record fixes the owner"
    INITIALIZING = "initialize the current run"


@dataclass(frozen=True)
class CompletionArtifacts:
    owner: staff.StaffRow
    run: Path
    access: ReviewRunAccess = ReviewRunAccess.RECORDED

    @cached_property
    def policy(self) -> EnrolledPaths | worker_panes.OrdinaryWorkerOwner:
        if self.owner.kind not in ("unit", "showrunner"):
            raise OSError("completion artifacts require a unit or showrunner owner")
        if self.owner.kind == "showrunner" and isinstance(self.owner.doc, staff.DocUnfilled):
            raise OSError("showrunner completion requires its staff document")
        return worker_panes.enrolled_worker_owner(self.owner)

    @property
    def protected(self) -> bool:
        return isinstance(self.policy, EnrolledPaths)

    def authorized_path(self, path: Path) -> Path:
        policy = self.policy
        if isinstance(policy, EnrolledPaths):
            if refusal := worker_panes.review_artifact_refusal(self.run, policy, path):
                raise OSError(refusal.reason)
            with self._directory(self.run):
                pass
            return path.absolute()
        return path

    def validate_status(self, passed: dict[str, object]) -> None:
        if destination := progress_history.dispatch_delivery(passed).actions.status_file:
            _ = self.authorized_path(Path(destination))

    @contextmanager
    def _directory(self, path: Path, *, create: bool = False) -> Generator[int]:
        """Open the recorded run first, then open each relative component."""
        root = self.run.absolute()
        path = path.absolute()
        if ".." in path.parts or not path.is_relative_to(root):
            raise OSError(f"{path}: the destination must stay in the recorded run")
        policy = self.policy
        if isinstance(policy, EnrolledPaths):
            if refusal := worker_panes.review_artifact_refusal(root, policy):
                raise OSError(refusal.reason)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        with ExitStack() as descriptors:
            parent = os.open(root.anchor, flags)
            _ = descriptors.callback(os.close, parent)
            for folder in root.parts[1:]:
                parent = os.open(folder, flags, dir_fd=parent)
                _ = descriptors.callback(os.close, parent)
            if isinstance(policy, EnrolledPaths):
                self._require_saved_run_owner(parent, policy)
            for folder in path.relative_to(root).parts:
                if create:
                    with suppress(FileExistsError):
                        os.mkdir(folder, dir_fd=parent)
                parent = os.open(folder, flags, dir_fd=parent)
                _ = descriptors.callback(os.close, parent)
            yield parent

    def _require_saved_run_owner(self, directory: int, policy: EnrolledPaths) -> None:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            fd = os.open(unit_runs.STATE_FILE, flags, dir_fd=directory)
        except FileNotFoundError:
            if self.access is ReviewRunAccess.INITIALIZING:
                if refusal := worker_panes.review_run_refusal(self.owner, self.run, policy):
                    raise OSError(refusal.reason) from None
                return
            raise
        with os.fdopen(fd, encoding="utf-8") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise OSError(f"{self.run}: expected a regular run record")
            fields = cast(object, json.load(stream))
        if not isinstance(fields, dict) or cast(dict[str, object], fields).get(unit_runs.OWNER_FIELD) != self.owner.id:
            raise OSError(f"{self.run}: the saved run owner differs from {self.owner.name}")

    @contextmanager
    def _parent(self, path: Path, *, create: bool = False) -> Generator[int]:
        if ".." in path.parts or path.absolute() == self.run.absolute():
            raise OSError(f"{path}: the destination must stay in the recorded run")
        with self._directory(path.absolute().parent, create=create) as parent:
            yield parent

    def mkdir(self, path: Path) -> None:
        if self.protected:
            with self._directory(path, create=True):
                pass
        else:
            path.mkdir(parents=True, exist_ok=True)

    def new_directory(self, parent: Path, name: str) -> Path:
        if not name or name in (".", "..") or Path(name).name != name:
            raise ValueError(f"invalid artifact directory name: {name}")
        with self._directory(parent, create=True) as directory:
            os.mkdir(name, dir_fd=directory)
        return parent / name

    @contextmanager
    def open_read(self, path: Path) -> Generator[TextIO]:
        if not self.protected:
            with path.open(encoding="utf-8") as stream:
                yield stream
            return
        with self._parent(path) as parent:
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            descriptor = os.open(path.name, flags, dir_fd=parent)
            with os.fdopen(descriptor, encoding="utf-8") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise OSError(f"{path}: expected a regular completion file")
                yield stream

    def read(self, path: Path) -> str:
        with self.open_read(path) as stream:
            return stream.read()

    def write(self, path: Path, text: str, *, create_only: bool = False) -> None:
        if self.protected:
            with self._parent(path, create=True) as parent:
                _write_review_artifact(parent, path, text.encode(), create_only=create_only)
        elif create_only:
            if not atomic_file.create_text_atomic(path, text):
                raise FileExistsError(f"{path}: the artifact already exists")
        else:
            _write_ordinary_artifact(path, text.encode())

    @contextmanager
    def append(self, path: Path) -> Generator[TextIO]:
        if not self.protected:
            with path.open("a", encoding="utf-8") as stream:
                yield stream
            return
        with self._parent(path, create=True) as parent:
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
            descriptor = os.open(path.name, flags, 0o666, dir_fd=parent)
            with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
                status = os.fstat(stream.fileno())
                if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
                    raise OSError(f"{path}: expected an unshared regular completion file")
                yield stream

    @contextmanager
    def lock(self, path: Path) -> Generator[TextIO]:
        with self.append(path) as stream:
            worker_lifecycle.acquire_flock(stream, mode=fcntl.LOCK_EX)
            yield stream

    def rename(self, source: Path, target: Path) -> None:
        if not self.protected:
            if source.is_file():
                _ = source.replace(target)
            return
        with self._parent(source) as source_parent, self._parent(target) as target_parent:
            before = _read_review_artifact(source_parent, source)
            destination = _read_review_artifact(target_parent, target)
            if isinstance(before, ArtifactAbsent):
                return
            if (_read_review_artifact(source_parent, source) != before
                    or _read_review_artifact(target_parent, target) != destination):
                raise OSError(f"{target}: an artifact changed before rename")
            os.replace(source.name, target.name, src_dir_fd=source_parent, dst_dir_fd=target_parent)
            os.fsync(source_parent)
            os.fsync(target_parent)

    def unlink(self, path: Path) -> None:
        if self.protected:
            with self._parent(path) as parent:
                os.unlink(path.name, dir_fd=parent)
        else:
            path.unlink()

    def reply(self, path: Path, text: str) -> None:
        if self.protected:
            self.write(path, text.rstrip() + "\n")
        else:
            save_reply(path, text)


def initializing_review_run_artifacts(owner: staff.StaffRow, run: Path) -> CompletionArtifacts:
    return CompletionArtifacts(owner, run, ReviewRunAccess.INITIALIZING)


@dataclass(frozen=True)
class StartAtDispatch:
    """Start the worker while dispatch holds its lock."""


START_AT_DISPATCH = StartAtDispatch()


class DispatchSubmission(IntEnum):
    """Submission leaves the pass active for its completion hook."""

    SUBMITTED = 0


class LaunchRow(IntEnum):
    CREATED = 1
    RETAINED = 2


def rows_before_launch() -> frozenset[StaffId]:
    return frozenset(row.id for row in staff.snapshot().rows)


def arm_launch_cleanup(cleanup: ExitStack, before: frozenset[StaffId],
                       result: worker_panes.WorkerStart) -> None:
    if isinstance(result, (worker_panes.OpenWorker, worker_panes.ProcessUnknownWorker, worker_panes.IncompleteStart)):
        if isinstance(result.created_pane, worker_panes.CreatedWorkerPane) and result.created_pane.terminal:
            row = LaunchRow.RETAINED if result.row.id in before else LaunchRow.CREATED
            _ = cleanup.callback(cleanup_failed_launch, result.row, result.pane, result.created_pane.terminal, row)


def cleanup_failed_launch(row: staff.StaffRow, pane: str, terminal: str, ownership: LaunchRow) -> None:
    """Close only the saved terminal and retire only a newly created row."""
    try:
        with redirect_stderr(io.StringIO()) as diagnostics:
            client = _client()
            closed = (client.close_tab_confirmed(pane, target=str(row.current_session), terminal=terminal)
                      and client.close_retained_pane(pane, terminal=terminal))
        if closed:
            if ownership is LaunchRow.CREATED:
                _ = staff.retire(row.id)
            return
        reason = " ".join(line.removeprefix("anomaly: ") for line in diagnostics.getvalue().splitlines())
        reason = reason or "pane close is incomplete"
    except (OSError, ValueError, herdr_types.HerdrFailure, subprocess.SubprocessError,
            staff.StaffRefused, staff.StaffUnreadable) as error:
        reason = str(error)
    print(f"anomaly: {pane}: cleanup failed: {' '.join(reason.splitlines())}", file=sys.stderr)


@dataclass(frozen=True)
class SendNoticesAfterDispatch:
    """This dispatch owns the lock and sends notices after its return."""


SEND_NOTICES_AFTER_DISPATCH = SendNoticesAfterDispatch()
COMPACTION_STARTED_TEXT = "compaction runs detached; the prompt follows"


class AttachedCompaction(Enum):
    RUN = "run compaction in this process"


def _accept_dispatch(parent: StaffId, row: staff.StaffRow, run: Path, instance: str, completion_id: str) -> None:
    progress_history.confirm_assignment_submission(run, instance, completion_id, worker_thread=str(row.current_session))
    handoff = worker_admission.dispatch_record(run, row.name)
    if handoff.state in (worker_admission.SwitchState.CREATION_INTENT_RECORDED,
                         worker_admission.SwitchState.DISPATCH_IN_FLIGHT):
        allowed = worker_admission.WorkerStartAllowed(row.name, parent, run, worker_admission.ExistingWorkerSlot(row))
        worker_admission.record_dispatch(allowed, handoff.assignment_id, worker_admission.SwitchState.ASSIGNMENT_ACCEPTED,
                                        expected_request=handoff.assignment_id)


def _done_instruction(dispatch_id: str) -> str:
    return (f"End your final reply with this line alone: DONE {dispatch_id}\n"
            "A reply that waits on a detached run is not final.")


def _compaction_lock(run: Path, completion_id: str) -> Path:
    return run / f"compact-{completion_id}.lock"


def _compaction_pending(run: Path, completion_id: str) -> bool:
    try:
        with _compaction_lock(run, completion_id).open("r") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except FileNotFoundError:
        pass
    return False


def _compaction_retry_pending(run: Path, selection: pass_recorder.DispatchSelection) -> bool:
    passed = selection.predecessor
    return (isinstance(selection.action, pass_recorder.MatchedDispatch)
            and isinstance(passed, pass_recorder.DispatchPredecessor)
            and bool(passed.completion_id) and _compaction_pending(run, passed.completion_id))


def _pending_compaction_result(run: Path, selection: pass_recorder.DispatchSelection) -> DispatchSubmission | ExitCode:
    try:
        if progress_history.pending_dispatch_confirmed(run, selection):
            return DispatchSubmission.SUBMITTED
    except pass_recorder.DispatchSelectionChanged as error:
        return _selection_changed(error)
    print("compaction helper submission is uncertain; the completion lock is held", file=sys.stderr)
    return ExitCode.UNCERTAIN


class CompactionStartupUncertain(Exception):
    """The launcher could not confirm whether the helper started."""


def _spawn_compaction(command: list[str], lock: file_lock.ExclusiveLock, log: Path) -> None:
    """Pass the lock to the helper and confirm that it reached its handler."""
    timeout = conf.read("direct").number("worker_settle_seconds")
    with ExitStack() as pipe:
        reader, writer = os.pipe()
        _ = pipe.callback(os.close, reader)
        try:
            with log.open("ab") as output:
                child = subprocess.Popen([*command, "--lock-fd", str(lock.descriptor), "--started-fd", str(writer)],
                                         pass_fds=(lock.descriptor, writer), start_new_session=True,
                                         stdin=subprocess.DEVNULL, stdout=output, stderr=output)
        finally:
            os.close(writer)
        deadline = time.monotonic() + timeout
        try:
            with selectors.DefaultSelector() as ready:
                _ = ready.register(reader, selectors.EVENT_READ)
                if not ready.select(max(0, deadline - time.monotonic())):
                    raise CompactionStartupUncertain(f"compaction helper startup timed out. Log {log}")
                if os.read(reader, 1):
                    return
            status = child.wait(timeout=max(0, deadline - time.monotonic()))
            if status == 0:
                raise CompactionStartupUncertain(f"compaction helper exited without a startup byte. Log {log}")
            raise herdr_types.HerdrFailure(f"exit {status} before startup")
        except (OSError, subprocess.TimeoutExpired) as error:
            raise CompactionStartupUncertain(f"compaction helper startup is uncertain. Log {log}. {error}") from error
        finally:
            if child.returncode is None:
                threading.Thread(target=child.wait, daemon=True).start()


def finish_dispatch_command(parent: StaffId, identity: StaffId, run: Path, instance: str, completion_id: str) -> list[str]:
    """Retry one recorded delivery."""
    return [sys.executable, str(Path(__file__).resolve()), "finish-dispatch", "--parent", str(parent),
            "--worker-id", str(identity), "--session-dir", str(run.resolve()), "--dispatch-id", instance,
            "--completion-id", completion_id]


def _detach_dispatch(parent: StaffId, identity: StaffId, run: Path, slot: str, instance: str, completion_id: str) -> None:
    command = finish_dispatch_command(parent, identity, run, instance, completion_id)
    log = run / f"compact-{instance}.log"
    lock = file_lock.take_exclusive(_compaction_lock(run, completion_id))
    if lock is file_lock.LockResult.BUSY:
        raise CompactionStartupUncertain(f"compaction helper submission is uncertain; the completion lock is held. Log {log}")
    try:
        _spawn_compaction(command, lock, log)
    except (OSError, conf.Unusable, herdr_types.HerdrFailure) as error:
        progress_history.fail_dispatch_start(run, slot, instance)
        raise herdr_types.HerdrFailure(f"compaction helper did not start: {error}. Log {log}") from error
    finally:
        lock.close()


def launched_agent(row: staff.StaffRow) -> progress_history.CalledAgent:
    """Read the family, model and effort the worker's pane launch kept; a launch with no kept model is not recorded."""
    try:
        entry = pane_record.launch_for_session(str(row.current_session))
        if entry is None:
            return progress_history.CALLED_AGENT_NOT_RECORDED
        kind = pane_launch.saved_kind(entry if "session_id" in entry else pane_record.resume_launch(entry))
        settings = worker_panes.kept_settings(models_registry.LaunchSettings(kind, "", "", ""),
                                              ["--model", entry["model"], *entry.get("codex_policy", [])])
    except (OSError, ValueError):
        return progress_history.CALLED_AGENT_NOT_RECORDED
    if not settings.model:
        return progress_history.CALLED_AGENT_NOT_RECORDED
    return progress_history.CalledAgent(kind, settings.model, settings.effort or "unset")


def _busy_dispatch(reason: str) -> ExitCode:
    print(reason, file=sys.stderr)
    return ExitCode.BUSY


def _prompt_conflict(conflict: pass_recorder.ActiveDispatchPromptConflict) -> ExitCode:
    return _busy_dispatch(f"{conflict.worker_name}: active pass {conflict.instance_id} has different prompt content; no prompt sent")


def _selection_changed(error: pass_recorder.DispatchSelectionChanged) -> ExitCode:
    return _busy_dispatch(f"{error}; no prompt sent")


class CloseCompletedPane(Enum):
    CLOSE = "close the completed pane"


class KeepCompletedPane(Enum):
    KEEP = "refresh the saved reply for an explicit retry"


@dataclass(frozen=True)
class ReuseCompletedPane:
    held_role: str


class CompletionNotices(Protocol):
    def recover_completion(self, row: staff.StaffRow, parent: staff.StaffRow, run: Path,
                           hold: worker_panes.WorkerLockHold, *, record: staff.StaffSnapshot,
                           pane: CloseCompletedPane | ReuseCompletedPane | KeepCompletedPane = CloseCompletedPane.CLOSE,
                           ) -> CompletionRecovery: ...

    def retry_completed_notice(self, worker_name: str, parent: staff.StaffRow, run: Path,
                               passed: dict[str, object]) -> Callable[[], None]: ...


def _completion_notices() -> CompletionNotices:
    return cast(CompletionNotices, cast(object, import_module("scripts.herdr.turn_end")))


def _require_review_order(owner: staff.StaffRow, role: WorkerRole | worker_panes.WorkerRoleUnavailable,
                          run: Path, *artifacts: Path) -> EnrolledPaths | worker_panes.OrdinaryWorkerOwner:
    paths = worker_panes.enrolled_worker_owner(owner)
    if isinstance(paths, EnrolledPaths):
        refusal = worker_panes.review_role_refusal(owner, role) or worker_panes.review_run_refusal(owner, run, paths, *artifacts)
        if refusal:
            raise ValueError(refusal.reason)
    return paths


def _review_artifacts(actions: progress_history.CompletionActions) -> tuple[Path, ...]:
    return tuple(Path(value) for value in (actions.summary_file, actions.status_file, actions.report_file,
                                           actions.style_directory) if value)


def _require_review_delivery(owner: staff.StaffRow, row: staff.StaffRow, run: Path,
                             passed: dict[str, object]) -> None:
    delivery = progress_history.dispatch_delivery(passed)
    artifacts = tuple(Path(value) for value in (passed.get("prompt_file"), delivery.reply_file, delivery.early_ready)
                      if isinstance(value, str) and value)
    writer = CompletionArtifacts(owner, run)
    if writer.protected:
        if refusal := worker_panes.review_role_refusal(owner, worker_panes.dispatch_role(row)):
            raise ValueError(refusal.reason)
        for path in (*artifacts, *_review_artifacts(delivery.actions)):
            _ = writer.authorized_path(path)
    writer.validate_status(passed)


def _require_running_review(owner: staff.StaffRow, client: herdr.Herdr, current: worker_panes.OpenWorker) -> None:
    paths = worker_panes.enrolled_worker_owner(owner)
    if isinstance(paths, EnrolledPaths):
        worker_panes.require_review_process(client, current.pane, paths)


@dataclass(frozen=True)
class CheckAssignmentAvailability:
    runs: unit_runs.LauncherRunDirectories
    status_file: str = ""

    @classmethod
    def for_worker(cls, record: staff.StaffSnapshot, row: staff.StaffRow, run: Path
                   ) -> CheckAssignmentAvailability:
        parent = record.parent_of(row)
        runs = (_assignment_runs(record, parent.id, row, additional=(run,)) if parent is not None
                else unit_runs.LauncherRunDirectories({}, {}))
        return cls(runs)

    def check(self, row: staff.StaffRow, hold: worker_panes.WorkerLockHold, *,
              retry: RetryingAssignment | AssignmentCheckStage,
              ) -> worker_panes.CloseAllowed | worker_panes.DispatchHoldsWorker:
        current_record = staff.snapshot()
        current = current_record.by_id(row.id)
        paths = self.runs.by_worker.get(row.id)
        if (current is None or not hold.owns(current) or current.current_session != row.current_session or not paths):
            return worker_panes.DispatchHoldsWorker("the worker changed before dispatch")
        if isinstance(retry, RetryingAssignment):
            passed = worker_panes.worker_pass(current, retry.run, record=current_record)
            delivery = pass_recorder.dispatch_delivery(passed)
            if (passed.get("instance_id") != retry.instance or passed.get("status") not in ("active", "refused")
                    or delivery.completion_id != retry.delivery.completion_id or delivery.line != retry.delivery.line
                    or delivery.reply_file != retry.delivery.reply_file):
                return worker_panes.DispatchHoldsWorker("the pass or delivery changed before dispatch")
        detached = _worker_detached_work(current_record, self.runs, (current.id,))
        with progress_history.dispatch_reads(current_record, paths):
            return _assignment_hold(current, current_record, detached, hold, status_file=self.status_file, retry=retry)


class AvailabilityAtUse(Enum):
    DIRECT_DISPATCH = "the caller uses direct dispatch"


def _same_dispatch_pass(matched: pass_recorder.MatchedDispatch | pass_recorder.NewAssignment,
                        passed: dict[str, object]) -> bool:
    return isinstance(matched, pass_recorder.MatchedDispatch) and matched.instance_id == passed.get("instance_id")


def _awaiting_dispatch_hook(passed: dict[str, object], *, same_pass: bool) -> bool:
    return (same_pass and passed.get("status") in ("active", "refused")
            and isinstance(progress_history.hook_completion(passed), progress_history.NoHookCompletion))


def _pending_dispatch_completion(row: staff.StaffRow, run: Path, passed: dict[str, object], *,
                                 same_pass: bool) -> CompletionRecovery:
    """Read the receipt that can prevent recovery before a new dispatch."""
    if _awaiting_dispatch_hook(passed, same_pass=same_pass):
        return CompletionRecovery.NO_PENDING_NOTICE
    delivery = pass_recorder.dispatch_delivery(passed)
    if not delivery.line:
        return CompletionRecovery.NO_PENDING_NOTICE
    completion = progress_history.hook_completion(passed)
    saved_done = isinstance(completion, progress_history.HookCompletion) and completion.outcome in ("done", "late_done")
    ended = passed.get("status") in ("completed", "error", "canceled", "interrupted")
    if isinstance(completion, progress_history.HookCompletion):
        if completion.outcome == "stopped" or saved_done and completion.finished:
            return CompletionRecovery.NO_PENDING_NOTICE
        if completion.outcome == "error" and completion.reply_file:
            try:
                reply = Path(completion.reply_file).read_text()
            except FileNotFoundError:
                reply = "\n".join(completion.details)
            saved_done = worker_admission.reply_ends_dispatch(reply, delivery.completion_id)
        if not saved_done and not ended:
            return CompletionRecovery.NO_PENDING_NOTICE
    elif not ended and passed.get("status") not in ("active", "refused"):
        return CompletionRecovery.NO_PENDING_NOTICE
    instance = pass_recorder.string(passed.get("instance_id"))
    completion_id = delivery.completion_id or (instance if ended and not saved_done else "")
    receipt = dispatch_receipt(row, delivery.line, completion_id, run=run, require_transcript=True)
    if isinstance(receipt, DispatchEvidenceUnknown):
        raise ValueError(receipt.reason)
    if isinstance(receipt, (DispatchWaitingForRuns, DispatchAwaitingReply)):
        waiting = waiting_recovery(receipt)
        if (waiting is CompletionRecovery.WAITING_ON_RUNS and isinstance(receipt, DispatchWaitingForRuns)
                and not saved_done and not isinstance(receipt.reply, (DispatchComplete, DispatchAwaitingReply))):
            return CompletionRecovery.NO_PENDING_NOTICE
        return waiting
    return CompletionRecovery.NO_PENDING_NOTICE


def _admit_completion(row: staff.StaffRow, recovery: CompletionRecovery) -> ExitCode:
    if recovery in (CompletionRecovery.WAITING_ON_RUNS, CompletionRecovery.WAITING_ON_REPLY,
                    CompletionRecovery.WAITING_ON_REAP):
        return _busy_dispatch(f"{row.name}: {recovery.value}")
    if recovery is CompletionRecovery.PENDING_CLOSE:
        print(f"{row.name}: {recovery.value}", file=sys.stderr)
        return ExitCode.UNCERTAIN
    return ExitCode.OK


def admit_dispatch(parent: StaffId, name: WorkerName | progress_history.DispatchTargetAtUse, role: WorkerRole, prompt: Path, *,
                   session_dir: Path, slot: str, phase: str, launcher_command: list[str],
                   work: progress_history.DispatchWork, prompt_source: Path | bytes, record: staff.StaffSnapshot,
                   lock: worker_panes.WorkerLockAtUse | worker_panes.WorkerLockHold = worker_panes.WorkerLockAtUse.ACQUIRE,
                   ) -> pass_recorder.DispatchSelection | ExitCode:
    """Check the proposed dispatch without changing files or sending a prompt."""
    selection = progress_history.select_dispatch(session_dir, slot=slot, phase=phase, role=role,
        prompt=prompt, launcher_command=launcher_command, work=work, target=name, prompt_source=prompt_source)
    if isinstance(selection, pass_recorder.ActiveDispatchPromptConflict):
        return _prompt_conflict(selection)
    # Under the lock, recovery precedes startup. Dispatch uses the startup observation.
    if isinstance(lock, worker_panes.WorkerLockHold) or isinstance(name, progress_history.DispatchTargetAtUse):
        return selection
    if _compaction_retry_pending(session_dir, selection):
        return selection
    row = worker_admission.find_worker(record, parent, name.value)
    if row is None:
        return selection
    observed = worker_panes.observe_worker(parent, row, record)
    pane = observed if isinstance(observed, worker_panes.OpenWorker) and not observed.status.idle else None
    if pane is not None:
        checked = worker_panes.check_worker_lock(pane.row, include_name=True)
        if isinstance(checked, worker_panes.Kept):
            return _busy_dispatch(checked.blocker)
        matched = selection.action
        read_receipt: Callable[[staff.StaffRow], DispatchReceipt] | Literal["pane only"] = "pane only"
        if isinstance(matched, pass_recorder.MatchedDispatch):
            passed = worker_panes.worker_pass(pane.row, session_dir, record=record)
            if passed.get("instance_id") == matched.instance_id:
                delivery = pass_recorder.dispatch_delivery(passed)
                if passed.get("status") != "completed" and delivery.line:
                    read_receipt = partial(dispatch_receipt, line=delivery.line, dispatch_id=delivery.completion_id, run=session_dir)
                if passed.get("status") == "completed":
                    pane = None
        if pane is not None:
            ready = _dispatch_ready(parent, pane, _client(), read_receipt=read_receipt)
            if isinstance(ready, ExitCode):
                return ready
    recorded = progress_history.recorded_dispatch_run(row.id)
    if isinstance(recorded, progress_history.RecordedDispatchRun):
        previous = worker_panes.worker_pass(row, recorded.directory, record=record)
        try:
            pending = _pending_dispatch_completion(row, recorded.directory, previous,
                same_pass=_same_dispatch_pass(selection.action, previous))
        except (OSError, ValueError, staff.StaffUnreadable) as error:
            print(f"anomaly: {row.name}: completion recovery for pass {previous.get('instance_id')}: {error}", file=sys.stderr)
            return ExitCode.UNCERTAIN
        admission = _admit_completion(row, pending)
        if admission is not ExitCode.OK:
            return admission
    return selection


def dispatch(parent: StaffId, name: WorkerName, role: WorkerRole, prompt: Path, dispatch_id: str = "", *,
             session_dir: Path, slot: str, phase: str, kind: str, launcher_command: list[str],
             lens: str = "", reply: Path | ReplyBesidePrompt = REPLY_BESIDE_PROMPT, fix_cause: str = "",
             fix_pass: int = 0, activity: str = "", early_ready: Path | SingleStageReview = SINGLE_STAGE_REVIEW,
             policy: worker_panes.LaunchPolicy | worker_panes.UseRolePolicy = worker_panes.UseRolePolicy.CURRENT,
             started: worker_panes.OpenWorker | StartAtDispatch = START_AT_DISPATCH,
             completion_actions: pass_recorder.CompletionActions = progress_history.NO_COMPLETION_ACTIONS,
             on_submission: Callable[[], object] = lambda: None,
             lock: worker_panes.WorkerLockHold | worker_panes.WorkerLockAtUse = worker_panes.WorkerLockAtUse.ACQUIRE,
             notices: ExitStack | SendNoticesAfterDispatch = SEND_NOTICES_AFTER_DISPATCH,
             new_pass_instance_id: uuid.UUID | None = None,
             availability: CheckAssignmentAvailability | AvailabilityAtUse = AvailabilityAtUse.DIRECT_DISPATCH,
             ) -> DispatchSubmission | ExitCode:
    """Record the hook destinations, submit one prompt, and return at once."""
    record = staff.snapshot()
    owner = record.by_id(parent)
    if owner is None:
        print(f"{parent}: dispatch owner is unavailable", file=sys.stderr)
        return ExitCode.REFUSED
    record_status = os.environ.get(progress_history.ASSIGN_STATUS_FILE_ENV, "") if kind == "assign" else ""
    destination = reply if isinstance(reply, Path) else prompt.with_name(prompt.stem + "_reply.txt")
    artifacts = (prompt, destination, *(path for path in (early_ready,) if isinstance(path, Path)),
                 *(Path(value) for value in (record_status,) if value), *_review_artifacts(completion_actions))
    try:
        enrolled = isinstance(_require_review_order(owner, role, session_dir, *artifacts), EnrolledPaths)
    except (OSError, ValueError, ProductionPathsError, staff.StaffRefused, staff.StaffUnreadable) as error:
        print(error, file=sys.stderr)
        return ExitCode.REFUSED
    prompt = prompt.absolute() if enrolled else prompt.resolve()
    if enrolled:
        if isinstance(reply, Path):
            reply = reply.absolute()
        if record_status and not completion_actions.status_file:
            status = progress_history.worker_artifact(Path(record_status), slot, name.value).absolute()
            completion_actions = replace(completion_actions, status_file=str(status))
    with ExitStack() as after_dispatch, ExitStack() as operations, ExitStack() as cleanup:
        notice_delivery = notices if isinstance(notices, ExitStack) else after_dispatch
        expected = (worker_admission.find_worker(staff.snapshot(), parent, name.value)
                    if isinstance(lock, worker_panes.WorkerLockAtUse) else None)
        hold = operations.enter_context(worker_panes.worker_lock(parent, name)) if isinstance(lock, worker_panes.WorkerLockAtUse) else lock
        if isinstance(hold, worker_panes.Kept):
            return _busy_dispatch(hold.blocker)
        record = staff.snapshot()
        row = worker_admission.find_worker(record, parent, name.value)
        if ((row is None and hold.bound) or (row is not None and not hold.owns(row))
                or (expected is not None and (row is None or row.id != expected.id
                                             or row.current_session != expected.current_session))):
            return _busy_dispatch(f"{name.value}: the worker changed before dispatch")
        selection = admit_dispatch(parent, name, role, prompt, session_dir=session_dir, slot=slot, phase=phase,
            launcher_command=launcher_command, work=progress_history.DispatchWork(kind, lens, fix_pass),
            prompt_source=prompt, record=record, lock=hold)
        if isinstance(selection, ExitCode):
            return selection
        matched = selection.action
        if (unit_runs.terminal_run(unit_runs.read_run_fields(session_dir))
                and not (isinstance(matched, progress_history.MatchedDispatch)
                         and progress_history.dispatch_completed(session_dir, selection.slot, matched.instance_id))):
            print(f"{session_dir}: the run has ended dispatch admission", file=sys.stderr)
            return ExitCode.REFUSED
        completion_notices = _completion_notices()
        recovered = CompletionRecovery.NO_PENDING_NOTICE
        if row is not None and (owner := record.parent_of(row)) is not None:
            recorded = progress_history.recorded_dispatch_run(row.id)
            if isinstance(recorded, progress_history.RecordedDispatchRun):
                previous = worker_panes.worker_pass(row, recorded.directory, record=record)
                try:
                    _require_review_delivery(owner, row, recorded.directory, previous)
                except (OSError, ValueError, ProductionPathsError, staff.StaffRefused, staff.StaffUnreadable) as error:
                    print(error, file=sys.stderr)
                    return ExitCode.REFUSED
                same_pass = _same_dispatch_pass(matched, previous)
                pane = KeepCompletedPane.KEEP if same_pass else ReuseCompletedPane(completion_actions.held_role)
                try:
                    if not _awaiting_dispatch_hook(previous, same_pass=same_pass):
                        recovered = completion_notices.recover_completion(row, owner, recorded.directory, hold,
                                                                         record=record, pane=pane)
                except (OSError, ValueError, herdr_types.HerdrFailure, subprocess.SubprocessError,
                        staff.StaffRefused, staff.StaffUnreadable) as error:
                    print(f"anomaly: {row.name}: completion recovery for pass {previous.get('instance_id')}: {error}",
                          file=sys.stderr)
                    return ExitCode.UNCERTAIN
                admission = _admit_completion(row, recovered)
                if admission is not ExitCode.OK:
                    return admission

        def refuse_new_assignment(target: staff.StaffRow, check: CheckAssignmentAvailability,
                                  lock: worker_panes.WorkerLockHold) -> bool:
            if isinstance(matched, pass_recorder.MatchedDispatch):
                return False
            state = check.check(target, lock, retry=AssignmentCheckStage.DELIVERY)
            if isinstance(state, worker_panes.DispatchHoldsWorker):
                print(f"workers unavailable: {target.name}: {state.blocker}", file=sys.stderr)
                return True
            return False

        if row is not None and not isinstance(matched, pass_recorder.MatchedDispatch):
            if isinstance(availability, AvailabilityAtUse):
                availability = CheckAssignmentAvailability.for_worker(record, row, session_dir)
            if refuse_new_assignment(row, availability, hold):
                return ExitCode.BUSY
        if (isinstance(matched, pass_recorder.MatchedDispatch) and matched.worker_name == name.value
                and row is not None):
            passed = worker_panes.worker_pass(row, session_dir)
            delivery = pass_recorder.dispatch_delivery(passed)
            if passed.get("instance_id") != matched.instance_id:
                print(f"{row.name}: the selected pass belongs to another worker", file=sys.stderr)
                return ExitCode.BUSY
            if _compaction_retry_pending(session_dir, selection):
                return _pending_compaction_result(session_dir, selection)
            if passed.get("status") == "completed":
                try:
                    progress_history.validate_dispatch_selection(session_dir, selection)
                except pass_recorder.DispatchSelectionChanged as error:
                    return _selection_changed(error)
                receipt = dispatch_receipt(row, delivery.line, delivery.completion_id, run=session_dir)
                if isinstance(receipt, DispatchComplete):
                    holder = record.by_id(parent)
                    if holder is None:
                        raise OSError(f"{parent}: completion owner is unavailable")
                    CompletionArtifacts(holder, session_dir).reply(Path(delivery.reply_file), receipt.reply)
                _ = notice_delivery.callback(completion_notices.retry_completed_notice(
                    row.name, worker_admission.parent(staff.snapshot(), run=session_dir), session_dir,
                    worker_panes.worker_pass(row, session_dir)))
                return DispatchSubmission.SUBMITTED
        if isinstance(started, StartAtDispatch):
            before = rows_before_launch()
            current_start = worker_panes.start_or_resume(parent, name, role, lock=hold, policy=policy)
            arm_launch_cleanup(cleanup, before, current_start)
        else:
            if not started.row.reports_to(parent) or not hold.owns(started.row):
                print("dispatch startup does not belong to this worker", file=sys.stderr)
                return ExitCode.REFUSED
            current_start = started
        if not isinstance(current_start, worker_panes.OpenWorker):
            return startup_result(name, current_start)
        started = current_start
        if not isinstance(matched, pass_recorder.MatchedDispatch):
            ready = _dispatch_ready(parent, started, _client())
            if not isinstance(ready, worker_panes.OpenWorker):
                assert isinstance(ready, ExitCode)
                return ready
            started = ready
        if row is None or row.id != started.row.id or isinstance(availability, AvailabilityAtUse):
            record = staff.snapshot()
            availability = CheckAssignmentAvailability.for_worker(record, started.row, session_dir)
            if refuse_new_assignment(started.row, availability, hold):
                return ExitCode.BUSY
        current = worker_admission.find_worker(staff.snapshot(), parent, name.value)
        if (current is None or current.id != started.row.id or not hold.owns(current)
                or current.current_session != started.row.current_session):
            print(f"{started.row.name}: the worker changed before pass creation", file=sys.stderr)
            return ExitCode.BUSY

        def submitted() -> None:
            _ = cleanup.pop_all()
            _ = on_submission()

        try:
            if enrolled and record_status:
                _ = os.environ.pop(progress_history.ASSIGN_STATUS_FILE_ENV, None)
            instance = progress_history.begin_dispatch(session_dir, slot=slot, phase=phase, role=role,
                worker_name=current.name, worker_id=current.id, launcher_command=launcher_command, prompt=prompt, kind=kind, lens=lens,
                fix_cause=fix_cause, fix_pass=fix_pass, activity=activity, called=launched_agent(started.row),
                new_pass_instance_id=new_pass_instance_id, selection=selection)
        except pass_recorder.DispatchPromptRefused as refused:
            return _prompt_conflict(refused.conflict)
        except pass_recorder.DispatchSelectionChanged as error:
            return _selection_changed(error)
        finally:
            if enrolled and record_status:
                os.environ[progress_history.ASSIGN_STATUS_FILE_ENV] = record_status
        if dispatch_id and dispatch_id not in (instance, worker_panes.stage_two_completion_id(instance)):
            print("dispatch id differs from the recorder pass", file=sys.stderr)
            return ExitCode.REFUSED
        saved = pass_recorder.dispatch_delivery(worker_panes.worker_pass(started.row, session_dir))
        if not saved.line:
            dispatch_id = dispatch_id or instance
            line = f"Read the unchanged prompt file {json.dumps(str(prompt))}. " + _done_instruction(dispatch_id)
            handoff = worker_admission.dispatch_record(session_dir, current.name)
            if handoff.state in (worker_admission.SwitchState.CREATION_INTENT_RECORDED, worker_admission.SwitchState.DISPATCH_IN_FLIGHT):
                extract = worker_admission.handoff_path(session_dir, started.row)
                if extract.is_file():
                    line = f"Read the transcript extract: {extract}.\n" + line
            destination = reply if isinstance(reply, Path) else prompt.with_name(prompt.stem + "_reply.txt")
            saved = pass_recorder.DispatchDelivery(line, dispatch_id, str(destination),
                str(early_ready) if isinstance(early_ready, Path) else "", completion_actions)
            progress_history.save_dispatch_delivery(session_dir, instance, saved)
            if enrolled and kind == "assign" and completion_actions.status_file:
                holder = record.by_id(parent)
                if holder is None:
                    raise OSError(f"{parent}: completion owner is unavailable")
                CompletionArtifacts(holder, session_dir).write(Path(completion_actions.status_file), "running\n")
        deferred = False

        def defer_compaction() -> None:
            nonlocal deferred
            _detach_dispatch(parent, started.row.id, session_dir, slot, instance, saved.completion_id)
            deferred = True
            print(f"{started.row.name}: {COMPACTION_STARTED_TEXT}")

        before_input = partial(availability.check, hold=hold,
                               retry=RetryingAssignment(session_dir.resolve(), instance, saved))
        try:
            result = dispatch_turn(parent, started, _client(), saved.line, saved.completion_id,
                                   run=session_dir, instance_id=instance, on_submission=submitted,
                                   defer_compaction=defer_compaction, before_input=before_input)
        except CompactionStartupUncertain as error:
            # The helper can still deliver the prompt after this launcher returns.
            submitted()
            print(f"{started.row.name}: {error}", file=sys.stderr)
            return ExitCode.UNCERTAIN
        if result is DispatchSubmission.SUBMITTED:
            submitted()
        elif result is ExitCode.BUSY:
            _ = cleanup.pop_all()
        if result is DispatchSubmission.SUBMITTED and not deferred:
            _accept_dispatch(parent, started.row, session_dir, instance, saved.completion_id)
        return result


def _dispatch_ready(parent: StaffId, current: worker_panes.OpenWorker, client: herdr.Herdr, *,
                    read_receipt: Callable[[staff.StaffRow], DispatchReceipt] | Literal["pane only"] = "pane only",
                    ) -> worker_panes.OpenWorker | DispatchSubmission | ExitCode:
    """Check for a receipt or wait for the pane to accept a prompt."""
    codex = isinstance(staff.backend(current.row), staff.CodexBackend)
    if current.status == herdr.AgentStatus.BLOCKED and codex:
        return _blocked_question(client, current)
    if callable(read_receipt):
        found = read_receipt(current.row)
        if isinstance(found, DispatchEvidenceUnknown):
            print(f"anomaly: {current.row.name}: {found.reason}; no prompt sent", file=sys.stderr)
            return ExitCode.UNCERTAIN
        if not isinstance(found, DispatchNotSubmitted):
            return DispatchSubmission.SUBMITTED
    if current.status == herdr.AgentStatus.WORKING:
        try:
            _ = client.wait_turn_end(current.pane, int(conf.read("direct").number("worker_settle_seconds") * 1000))
            observed = worker_panes.observe_worker(parent, current.row, staff.snapshot())
        except herdr.AgentBlocked as error:
            if codex:
                observed = worker_panes.observe_worker(parent, current.row, staff.snapshot())
                if isinstance(observed, worker_panes.OpenWorker):
                    return _blocked_question(client, observed)
            print(f"{error}; no prompt sent", file=sys.stderr)
            return ExitCode.UNCERTAIN
        except (OSError, herdr_types.HerdrFailure, subprocess.TimeoutExpired) as error:
            print(f"{error}; no prompt sent", file=sys.stderr)
            return ExitCode.UNCERTAIN
        if not isinstance(observed, worker_panes.OpenWorker):
            print(f"{current.row.name}: worker state is unavailable; no prompt sent", file=sys.stderr)
            return ExitCode.UNCERTAIN
        current = observed
        if current.status == herdr.AgentStatus.BLOCKED and codex:
            return _blocked_question(client, current)
        if callable(read_receipt):
            found = read_receipt(current.row)
            if isinstance(found, DispatchEvidenceUnknown):
                print(f"{current.row.name}: {found.reason}; no prompt sent", file=sys.stderr)
                return ExitCode.UNCERTAIN
            if not isinstance(found, DispatchNotSubmitted):
                return DispatchSubmission.SUBMITTED
    if not current.status.idle:
        return _busy_dispatch(f"{current.row.name}: agent is {current.status.value}")
    return current


def dispatch_turn(parent: StaffId, started: worker_panes.OpenWorker, client: herdr.Herdr,
                   line: str, dispatch_id: str, *, run: Path, instance_id: str,
                   before_input: Callable[[staff.StaffRow], worker_panes.CloseAllowed | worker_panes.DispatchHoldsWorker],
                   on_submission: Callable[[], object] = lambda: None,
                   defer_compaction: Callable[[], None] | AttachedCompaction = AttachedCompaction.RUN,
                   ) -> DispatchSubmission | ExitCode:
    """Submit the saved prompt after the bounded idle checks."""
    record = staff.snapshot()
    owner = record.by_id(parent)
    if owner is not None:
        try:
            paths = worker_panes.enrolled_worker_owner(owner)
            if isinstance(paths, EnrolledPaths):
                _require_review_delivery(owner, started.row, run, worker_panes.worker_pass(started.row, run, record=record))
                worker_panes.require_review_process(client, started.pane, paths)
        except (OSError, ValueError, ProductionPathsError, herdr_types.HerdrFailure, subprocess.SubprocessError,
                staff.StaffRefused, staff.StaffUnreadable) as error:
            print(error, file=sys.stderr)
            return ExitCode.REFUSED
    if progress_history.dispatch_submission_confirmed(run, instance_id, dispatch_id):
        return DispatchSubmission.SUBMITTED
    read_receipt = partial(dispatch_receipt, line=line, dispatch_id=dispatch_id, run=run)
    current = _dispatch_ready(parent, started, client, read_receipt=read_receipt)
    if not isinstance(current, worker_panes.OpenWorker):
        return current
    codex = isinstance(staff.backend(current.row), staff.CodexBackend)

    def input_allowed() -> bool:
        state = before_input(current.row)
        if isinstance(state, worker_panes.DispatchHoldsWorker):
            print(f"workers unavailable: {current.row.name}: {state.blocker}", file=sys.stderr)
            return False
        return True

    if not input_allowed():
        return ExitCode.BUSY
    try:
        if callable(defer_compaction) and isinstance(_compaction_plan(current.row), CompactionRequired):
            defer_compaction()
            return DispatchSubmission.SUBMITTED
        _compact_before_dispatch(client, current)
    except (OSError, ValueError, herdr_types.HerdrFailure, subprocess.SubprocessError) as error:
        reason = " ".join(str(error).splitlines())
        try:
            composer = client.composer(current.pane)
            if isinstance(composer, herdr.ComposerInput) and composer.text.strip() == "/compact":
                client.send_keys(current.pane, "ctrl-u", target=str(current.row.current_session), expected_text="/compact")
        except (OSError, herdr_types.HerdrFailure, subprocess.SubprocessError) as cleanup_error:
            reason += f"; compaction input cleanup failed: {' '.join(str(cleanup_error).splitlines())}"
        print(f"{current.row.name}: compaction failed: {reason}; no prompt sent", file=sys.stderr)
        return ExitCode.REFUSED
    if not input_allowed():
        return ExitCode.BUSY
    try:
        _agent_command(client, current, "prompt", line, on_submission=on_submission)
    except OSError as error:
        reason = " ".join(str(error).splitlines())
        raise OSError(f"{reason}; prompt delivered to pane {current.pane}") from error
    except (herdr_types.HerdrFailure, subprocess.TimeoutExpired) as error:
        if isinstance(error, herdr.AgentBlocked) and codex:
            return _blocked_question(client, current, delivered_to=current.pane)
        reason = " ".join(str(error).splitlines())
        print(f"{reason}; prompt delivered to pane {current.pane}", file=sys.stderr)
        return ExitCode.UNCERTAIN
    if codex:
        deadline = time.monotonic() + conf.read("direct").number("worker_settle_seconds")
        while True:
            receipt = dispatch_receipt(current.row, line, dispatch_id, run=run)
            if not isinstance(receipt, (DispatchNotSubmitted, DispatchEvidenceUnknown)):
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                if isinstance(client.pane_activity(current.pane), herdr.PaneAtShell):
                    break
            except (OSError, herdr_types.HerdrFailure, subprocess.TimeoutExpired):
                pass
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        if isinstance(receipt, (DispatchNotSubmitted, DispatchEvidenceUnknown)):
            reason = receipt.reason if isinstance(receipt, DispatchEvidenceUnknown) else "dispatch prompt is not in the transcript"
            print(f"anomaly: {current.row.name}: {' '.join(reason.splitlines())}; submission is unproved", file=sys.stderr)
            return ExitCode.UNCERTAIN
    return DispatchSubmission.SUBMITTED


def _blocked_question(client: herdr.Herdr, worker: worker_panes.OpenWorker, *, delivered_to: str = "") -> ExitCode:
    print(f"question: {worker.row.name} pane {worker.pane}", file=sys.stderr)
    try:
        question = client.read(worker.pane, 40, joined=False, ansi=False)
    except (herdr_types.HerdrFailure, OSError, subprocess.SubprocessError) as error:
        reason = f"anomaly: {worker.row.name}: {' '.join(str(error).splitlines())}"
        if delivered_to:
            reason += f"; prompt delivered to pane {delivered_to}"
        print(reason, file=sys.stderr)
    else:
        print(question.rstrip("\n"), file=sys.stderr)
    return ExitCode.BLOCKED


class PreparedDirectorNotice(Protocol):
    def send_notice(self) -> None: ...


def _dispatch_notice(parent: StaffId, identity: StaffId, run: Path, instance: str,
                     passed: dict[str, object], outcome: Literal["busy", "error", "uncertain"], detail: str) -> None:
    """Send the existing director notice without completing the worker turn."""
    record = staff.snapshot()
    owner = record.by_id(parent)
    if owner is None:
        raise ValueError(f"{parent}: notice recipient is not in staff")
    row = record.by_id(identity)
    name = row.name if row is not None else str(passed.get("team_slot", identity))
    module = import_module("scripts.herdr.turn_end")
    prepare = cast(Callable[[str, staff.StaffRow, Path, dict[str, object], str, object, Path, str, str],
                            PreparedDirectorNotice], module._prepare_notice)
    turn_outcome = cast(Callable[[str], object], module.TurnOutcome)(outcome)
    prepare(name, owner, run, passed, instance, turn_outcome, run, "", detail).send_notice()


def _stopped_notice_settled(run: Path, instance: str) -> bool:
    # The notice record imports send, so load it only on this path.
    from scripts.herdr import pass_notices

    return pass_notices.is_settled(run, instance, "stopped")


def _dispatch_failure_step(identity: StaffId, step: str, action: Callable[[], None]) -> bool:
    """Report each failed step and return whether it succeeds."""
    try:
        action()
        return True
    except (Exception, SystemExit) as error:
        print(f"anomaly: {identity}: dispatch {step} failed: {' '.join(str(error).splitlines())}", file=sys.stderr)
        return False


class CompactionLockAtUse(Enum):
    ACQUIRE = "acquire the lock for a direct invocation"


def finish_dispatch(parent: StaffId, identity: StaffId, run: Path, instance: str, completion_id: str, *,
                    lock: file_lock.ExclusiveLock | CompactionLockAtUse = CompactionLockAtUse.ACQUIRE,
                    started_fd: int = -1) -> int:
    """Hold the compaction lock until the detached dispatch ends."""
    if started_fd >= 0:
        try:
            # The launcher can close its pipe after the startup deadline.
            with suppress(BrokenPipeError):
                _ = os.write(started_fd, b"1")
        finally:
            os.close(started_fd)
    with ExitStack() as notices, ExitStack() as completion_lock:
        if isinstance(lock, file_lock.ExclusiveLock):
            _ = completion_lock.callback(lock.close)
            os.set_inheritable(lock.descriptor, False)
        return _finish_dispatch(parent, identity, run, instance, completion_id, lock, completion_lock, notices)


def _finish_dispatch(parent: StaffId, identity: StaffId, run: Path, instance: str, completion_id: str,
                     lock: file_lock.ExclusiveLock | CompactionLockAtUse, completion_lock: ExitStack,
                     notices: ExitStack) -> int:
    path = run / f"compact-{instance}_dispatch_error.txt"
    diagnostics = io.StringIO()
    result: DispatchSubmission | ExitCode = ExitCode.UNCERTAIN
    submission_unproved = False

    def prompt_input_possible() -> None:
        nonlocal submission_unproved
        submission_unproved = True

    def current_pass() -> dict[str, object]:
        no_pass: dict[str, object] = {}
        return next((item for item in _pass_records(worker_panes.run_state(run))
                     if item.get("instance_id") == instance and item.get("worker_id") == identity
                     and item.get("status") == "active"
                     and pass_recorder.dispatch_delivery(item).completion_id == completion_id), no_pass)

    def busy(passed: dict[str, object], detail: str) -> ExitCode:
        if not passed or progress_history.dispatch_submission_confirmed(run, instance, completion_id):
            return ExitCode.OK
        print(detail, file=sys.stderr)
        _ = notices.callback(_dispatch_failure_step, identity, "notice",
            partial(_dispatch_notice, parent, identity, run, instance, passed, "busy", detail))
        return ExitCode.BUSY

    confirmation_error: Exception | SystemExit | None = None
    try:
        if progress_history.dispatch_submission_confirmed(run, instance, completion_id):
            return ExitCode.OK
    except (Exception, SystemExit) as error:
        confirmation_error = error

    if isinstance(lock, CompactionLockAtUse):
        try:
            acquired = completion_lock.enter_context(
                file_lock.exclusive(_compaction_lock(run, completion_id), wait=False))
        except (Exception, SystemExit) as error:
            print(f"{identity}: cannot acquire the compaction lock: {error}", file=sys.stderr)
            return ExitCode.UNCERTAIN
        if acquired is not file_lock.LockResult.ACQUIRED:
            print(f"{identity}: the compaction lock is busy", file=sys.stderr)
            return ExitCode.BUSY

    try:
        if confirmation_error is not None:
            raise confirmation_error
        if isinstance(lock, CompactionLockAtUse):
            if progress_history.dispatch_submission_confirmed(run, instance, completion_id):
                return ExitCode.OK
        expected = staff.snapshot().by_id(identity)
        if expected is None or expected.kind != "worker" or not expected.reports_to(parent):
            return busy(current_pass(), f"{identity}: the worker is unavailable for this unit")
        name = WorkerName.parse(expected.name)
        with worker_panes.worker_lock(parent, name, blocking=True) as hold:
            if progress_history.dispatch_submission_confirmed(run, instance, completion_id):
                return ExitCode.OK
            passed = current_pass()
            if not passed:
                return ExitCode.OK
            if isinstance(hold, worker_panes.Kept):
                return busy(passed, hold.blocker)
            record = staff.snapshot()
            row = worker_admission.find_worker(record, parent, name.value)
            if (row is None or row.id != identity or not hold.owns(row)
                    or row.current_session != expected.current_session):
                return busy(passed, f"{expected.name}: the worker changed before dispatch")
            if worker_panes.worker_pass(row, run, record=record).get("instance_id") != instance:
                return busy(passed, f"{row.name}: the dispatch pass is unavailable for this worker")
            owner = record.by_id(parent)
            if owner is None:
                return busy(passed, f"{parent}: dispatch owner is unavailable")
            try:
                _require_review_delivery(owner, row, run, passed)
            except (OSError, ValueError, ProductionPathsError, staff.StaffRefused, staff.StaffUnreadable) as error:
                print(error, file=sys.stderr)
                return ExitCode.REFUSED
            delivery = pass_recorder.dispatch_delivery(passed)
            if delivery.reply_file:
                reply = Path(delivery.reply_file)
                path = reply.with_name(reply.stem + "_dispatch_error" + reply.suffix)
            current = worker_panes.observe_worker(parent, row, record)
            if not isinstance(current, worker_panes.OpenWorker):
                return busy(passed, f"{row.name}: worker state is unavailable; no prompt sent")

            with redirect_stderr(diagnostics):
                availability = CheckAssignmentAvailability.for_worker(record, row, run)
                before_input = partial(availability.check, hold=hold,
                                       retry=RetryingAssignment(run.resolve(), instance, delivery))
                result = dispatch_turn(parent, current, _client(), delivery.line, completion_id, run=run,
                                       instance_id=instance, before_input=before_input,
                                       on_submission=prompt_input_possible)
                if result is DispatchSubmission.SUBMITTED:
                    prompt_input_possible()
                    _accept_dispatch(parent, row, run, instance, completion_id)
                    return result
        detail = diagnostics.getvalue().strip() or f"dispatch failed with exit {int(result)}"
        if result is ExitCode.BUSY:
            return busy(passed, detail)
        submission_unproved = submission_unproved or result is ExitCode.UNCERTAIN
    except recorder_state.OldRecorderState as error:
        print(str(error), file=sys.stderr)
        return ExitCode.UNCERTAIN
    except (Exception, SystemExit) as error:
        result = ExitCode.UNCERTAIN
        detail = diagnostics.getvalue().strip()
        detail = (detail + "\n" if detail else "") + str(error)

    def end_pass() -> None:
        slot = str(passed.get("team_slot", ""))
        if not slot:
            raise ValueError(f"{instance}: dispatch pass is unavailable")
        progress_history.complete_dispatch(run, slot, instance, status="error")

    def notice() -> None:
        if not completion_saved or not reply_saved:
            _dispatch_notice(parent, identity, run, instance, passed, "error", detail)
            return
        row = staff.snapshot().by_id(identity)
        if row is None:
            _dispatch_notice(parent, identity, run, instance, passed, "error", detail)
        else:
            notify = cast(Callable[[StaffId], detached_runs.CompletionReplay],
                          import_module("scripts.herdr.turn_end").run_finished)
            _ = notify(row.id)

    try:
        with worker_panes.worker_id_lock(identity, blocking=True):
            if progress_history.dispatch_submission_confirmed(run, instance, completion_id):
                return ExitCode.OK
            passed = current_pass()
            if not passed:
                return ExitCode.OK
            row = staff.snapshot().by_id(identity)
            if (row is None or row.kind != "worker" or not row.reports_to(parent)
                    or worker_panes.worker_pass(row, run).get("instance_id") != instance):
                return busy(passed, f"{identity}: the worker is unavailable for this unit")
            print(detail, file=sys.stderr)
            if submission_unproved:
                _ = notices.callback(_dispatch_failure_step, identity, "notice",
                                     partial(_dispatch_notice, parent, identity, run, instance, passed,
                                             "uncertain", detail))
                return result
            _ = _dispatch_failure_step(identity, "pass end", end_pass)
            completion_saved = _dispatch_failure_step(identity, "hook completion", lambda: progress_history.save_hook_completion(
                run, instance, progress_history.HookCompletion("error", str(path), (detail,))))
            def save_error_reply() -> None:
                holder = staff.snapshot().by_id(parent)
                if holder is None:
                    raise OSError(f"{parent}: completion owner is unavailable")
                CompletionArtifacts(holder, run).reply(path, detail)

            reply_saved = _dispatch_failure_step(identity, "error reply", save_error_reply)
            _ = notices.callback(_dispatch_failure_step, identity, "notice", notice)
    except (Exception, SystemExit) as error:
        print(f"{identity}: cannot recover dispatch {instance}: {error}", file=sys.stderr)
        return ExitCode.UNCERTAIN
    return result


def _role_number(name: str, role: WorkerRole) -> int:
    if name == role:
        return 1
    suffix = name.removeprefix(role)
    return int(suffix) if name.startswith(role) and suffix.isdigit() and int(suffix) >= 2 else 0


def _worker_detached_work(record: staff.StaffSnapshot, runs: unit_runs.LauncherRunDirectories,
                          workers: Iterable[StaffId]) -> session_state.RunningDetachedWork:
    directories = unit_runs.LauncherRunDirectories({identity: runs.by_worker[identity]
        for identity in workers if identity in runs.by_worker}, runs.by_session)
    return session_state.running_detached_work(record, worker_runs=directories)


def _worker_dispatch_state(row: staff.StaffRow, record: staff.StaffSnapshot, detached: session_state.RunningDetachedWork,
                           lock: worker_panes.WorkerLockHold | worker_panes.WorkerLockAtUse = worker_panes.WorkerLockAtUse.ACQUIRE, *,
                           read: Callable[[Path], dict[str, object]] = worker_panes.run_state,
                           ) -> worker_panes.WorkerDispatchState:
    run, *related = detached.launcher_runs.by_worker[row.id]
    try:
        return worker_panes.worker_dispatch_state(row, run, lock, detached=detached, record=record,
                                                  related_runs=related, read=read)
    except (OSError, ValueError) as error:
        detail = " ".join(str(error).splitlines())
        unit_runs.report_anomaly(f"anomaly: {row.name}: {detail}")
        return worker_panes.DispatchHoldsWorker(detail)


def _waiting_on_done(row: staff.StaffRow, detached: session_state.RunningDetachedWork,
                     record: staff.StaffSnapshot) -> bool:
    run = detached.launcher_runs.by_worker[row.id][0]
    pending = worker_lifecycle.read_json_object(pending_done_path(run, row.id))
    if not pending:
        return False
    passed = worker_panes.worker_pass(row, run, record=record)
    return (pending.get("instance") == passed.get("instance_id")
            and pending.get("stage") == pass_recorder.dispatch_delivery(passed).completion_id)


def _assignment_runs(record: staff.StaffSnapshot, parent: StaffId, target: WorkerRole | WorkerName | staff.StaffRow, *,
                     additional: tuple[Path, ...] = ()) -> unit_runs.LauncherRunDirectories:
    rows = tuple(row for row in record.rows if row.kind != "worker" or (row.reports_to(parent)
                 and (row.id == target.id if isinstance(target, staff.StaffRow)
                      else row.name == target.value if isinstance(target, WorkerName) else _role_number(row.name, target))))
    runs = worker_panes.worker_run_directories(replace(record, rows=rows))
    return unit_runs.LauncherRunDirectories({identity: tuple(dict.fromkeys((*paths, *additional)))
        for identity, paths in runs.by_worker.items()}, runs.by_session)


class AssignmentCheckStage(Enum):
    SELECTION = "the caller selects a candidate"
    DELIVERY = "completion recovery ran under the worker lock"


@dataclass(frozen=True)
class RetryingAssignment:
    run: Path
    instance: str
    delivery: pass_recorder.DispatchDelivery


def _assignment_hold(row: staff.StaffRow, record: staff.StaffSnapshot, detached: session_state.RunningDetachedWork,
                     lock: worker_panes.WorkerLockHold | worker_panes.WorkerLockAtUse = worker_panes.WorkerLockAtUse.ACQUIRE,
                     *, status_file: str = "",
                     retry: RetryingAssignment | AssignmentCheckStage = AssignmentCheckStage.DELIVERY,
                     ) -> worker_panes.DispatchHoldsWorker | worker_panes.CloseAllowed:
    """Check for work that blocks a new assignment or the exact retry."""
    @cache
    def read(directory: Path) -> dict[str, object]:
        state = worker_panes.run_state(directory)
        if isinstance(retry, RetryingAssignment) and directory.resolve() == retry.run:
            remaining = {str(index): passed for index, passed in enumerate(_pass_records(state))
                         if passed.get("instance_id") != retry.instance or passed.get("worker_id") != row.id}
            return {**state, "pass": remaining}
        return state

    def reply_checks(directory: Path) -> dict[str, object]:
        state = read(directory)
        passes = _pass_records(state)
        stopped = [passed for passed in passes if passed.get("worker_id") == row.id
                   and passed.get("status") == "canceled"
                   and isinstance(hook := progress_history.hook_completion(passed), progress_history.HookCompletion)
                   and hook.outcome == "stopped"]
        if not stopped:
            return state
        receipts = {receipt.instance_id: receipt for receipt in progress_history.stopped_pass_receipts(directory, row.id)}
        reusable: set[str] = set()
        for passed in stopped:
            instance = passed.get("instance_id")
            receipt = receipts.get(instance) if isinstance(instance, str) else None
            delivery = pass_recorder.dispatch_delivery(passed)
            if receipt is None or receipt.completion_id != delivery.completion_id or receipt.line != delivery.line:
                continue
            result = dispatch_receipt(row, receipt.line, receipt.completion_id, run=directory, pane_pass=receipt,
                                      require_transcript=True)
            if isinstance(result, DispatchEvidenceUnknown):
                raise ValueError(f"completion evidence is unavailable: {result.reason}")
            reusable.add(receipt.instance_id)
        if not reusable:
            return state
        return {**state, "pass": {str(index): passed for index, passed in enumerate(passes)
                                if passed.get("worker_id") != row.id or passed.get("instance_id") not in reusable}}

    state = _worker_dispatch_state(row, record, detached, lock, read=reply_checks)
    if isinstance(state, worker_panes.DispatchHoldsWorker):
        return state
    statuses = {status_file} if status_file and not isinstance(retry, RetryingAssignment) else set[str]()
    for directory in detached.launcher_runs.by_worker[row.id]:
        for passed in _pass_records(read(directory)):
            if passed.get("worker_id") != row.id:
                continue
            if passed.get("status") in ("active", "refused"):
                if (retry is AssignmentCheckStage.SELECTION and isinstance(state, worker_panes.CompletedWorkerPass)
                        and passed.get("instance_id") == state.dispatch_id):
                    continue
                return worker_panes.DispatchHoldsWorker("a pass is open")
            saved = passed.get("assign_status_file") or pass_recorder.dispatch_delivery(passed).actions.status_file
            if isinstance(saved, str) and saved:
                statuses.add(saved)
    for status in statuses:
        try:
            if Path(status).read_text(encoding="utf-8").strip() == "running":
                return worker_panes.DispatchHoldsWorker("the assignment status is running")
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError) as error:
            return worker_panes.DispatchHoldsWorker(f"the assignment status is unavailable: {error}")
    return worker_panes.CloseAllowed()


def select_worker(parent: StaffId, role: WorkerRole, record: staff.StaffSnapshot, *,
                  runs: unit_runs.LauncherRunDirectories | None = None, additional_runs: tuple[Path, ...] = ()) -> (
        WorkerName | worker_panes.StartRefused | worker_panes.ProcessUnknownWorker | worker_panes.UncertainWorker):
    """Select a proved idle worker, a resumable worker, or the lowest free name."""
    names = [WorkerName.parse(row.name) for row in record.find(kind="worker", parent=parent)
             if _role_number(row.name, role)]
    candidates = worker_panes.observe_selected(parent, record, names)
    candidates.sort(key=lambda item: _role_number(item.row.name, role))
    reusable = [item for item in candidates if worker_panes.worker_state(item) == "idle"]
    if reusable:
        runs = _assignment_runs(record, parent, role, additional=additional_runs) if runs is None else runs
        detached = _worker_detached_work(record, runs, (item.row.id for item in reusable))
        directories = dict.fromkeys(run for paths in detached.launcher_runs.by_worker.values() for run in paths)
        with progress_history.dispatch_reads(record, directories):
            for item in reusable:
                state = _assignment_hold(item.row, record, detached, retry=AssignmentCheckStage.SELECTION)
                if isinstance(state, worker_panes.CloseAllowed):
                    return WorkerName.parse(item.row.name)
    for item in candidates:
        if isinstance(item, (worker_panes.ProcessUnknownWorker, worker_panes.UncertainWorker)):
            return item
    if len(candidates) > len(reusable):
        unavailable = ", ".join(item.row.name for item in candidates)
        return worker_panes.StartRefused(worker_panes.StartRefusal.BUSY_ROLE_CHANGE, f"workers unavailable: {unavailable}")
    return _free_name(parent, role, record)


def _free_name(parent: StaffId, role: WorkerRole, record: staff.StaffSnapshot) -> WorkerName:
    names = {row.name for row in record.find(kind="worker", parent=parent)}
    number = 1
    while (name := role + (str(number) if number > 1 else "")) in names:
        number += 1
    return WorkerName.parse(name)


def launch(target: worker_panes.StaffWorker | worker_panes.DurableDesignRole,
           policy: worker_panes.LaunchPolicy, *,
           lock: worker_panes.WorkerLockHold | worker_panes.WorkerLockAtUse = worker_panes.WorkerLockAtUse.ACQUIRE,
           ) -> worker_panes.WorkerStart:
    """Start an explicit staff target with the supplied policy."""
    with staff.row_operation(target.parent):
        return worker_panes.start_or_resume(target.parent, target.name, target.role, policy=policy, lock=lock)


def add(parent: StaffId, role: WorkerRole, *, name: str = "") -> worker_panes.WorkerStart:
    with staff.row_operation(parent):
        return worker_panes.start_or_resume(parent, WorkerName.parse(name) if name else _free_name(parent, role, staff.snapshot()), role)


@herdr.pane_evidence_run()
def assign(parent: StaffId, role: WorkerRole, prompt: Path, *, session_dir: Path, slot: str,
           phase: str, kind: str, launcher_command: list[str], lens: str = "", name: str = "",
           reply: Path | ReplyBesidePrompt = REPLY_BESIDE_PROMPT, dispatch_id: str = "", fix_cause: str = "",
           fix_pass: int = 0, activity: str = "", early_ready: Path | SingleStageReview = SINGLE_STAGE_REVIEW,
           policy: worker_panes.UseRolePolicy | worker_panes.LaunchPolicy = worker_panes.UseRolePolicy.CURRENT,
           completion_actions: pass_recorder.CompletionActions = progress_history.NO_COMPLETION_ACTIONS,
           new_pass_instance_id: uuid.UUID | None = None) -> DispatchSubmission | ExitCode:
    with ExitStack() as cleanup, ExitStack() as notices, ExitStack() as operations:
        record = staff.snapshot()
        if not name:
            source = prompt.resolve()
            selection = admit_dispatch(parent, progress_history.DispatchTargetAtUse.FROM_LAUNCHER, role, source,
                session_dir=session_dir, slot=slot, phase=phase, launcher_command=launcher_command,
                work=progress_history.DispatchWork(kind, lens, fix_pass), prompt_source=source, record=record)
            if isinstance(selection, ExitCode):
                return selection
            if isinstance(selection.action, pass_recorder.MatchedDispatch):
                name = selection.action.worker_name
        rows = record.find(kind="worker", parent=parent)
        owner = record.by_id(parent)
        enrolled = False
        if owner is not None:
            artifacts = (prompt, *(path for path in (reply, early_ready) if isinstance(path, Path)),
                         *_review_artifacts(completion_actions))
            try:
                enrolled = isinstance(_require_review_order(owner, role, session_dir, *artifacts), EnrolledPaths)
            except (OSError, ValueError, ProductionPathsError, staff.StaffRefused, staff.StaffUnreadable) as error:
                print(error, file=sys.stderr)
                return ExitCode.REFUSED
        needs_team = False
        if not enrolled and not name and role in ("impl", "fix", "review", "test"):
            needs_team = not any(isinstance(worker_panes.dispatch_role(row), str) for row in rows)
        if needs_team:
            names = {row.name for row in rows}
            for default_role in ("impl", "fix", "review", "test"):
                if default_role in names or default_role == role:
                    continue
                before = rows_before_launch()
                with redirect_stderr(io.StringIO()) as diagnostics:
                    result = worker_panes.start_or_resume(parent, WorkerName.parse(default_role), default_role)
                if isinstance(result, worker_panes.OpenWorker):
                    arm_launch_cleanup(cleanup, before, result)
                    print(diagnostics.getvalue(), end="", file=sys.stderr)
                    continue
                with ExitStack() as failed:
                    arm_launch_cleanup(failed, before, result)
                detail = (result.detail if isinstance(result, worker_panes.StartRefused) else
                          result.cause if isinstance(result, worker_panes.UnpublishedWorker) else result.reason)
                print(f"anomaly: {default_role}: {detail}", file=sys.stderr)
            record = staff.snapshot()
        runs = unit_runs.LauncherRunDirectories({}, {}) if name else _assignment_runs(
            record, parent, role, additional=(session_dir,))
        chosen = WorkerName.parse(name) if name else select_worker(parent, role, record, runs=runs)
        if not isinstance(chosen, WorkerName):
            if isinstance(chosen, worker_panes.StartRefused) and chosen.reason is worker_panes.StartRefusal.BUSY_ROLE_CHANGE:
                print(f"{chosen.reason.value}: {chosen.detail}", file=sys.stderr)
                return ExitCode.BUSY
            return startup_result(WorkerName.parse(role), chosen)
        selected_row = worker_admission.find_worker(record, parent, chosen.value)
        hold = operations.enter_context(worker_panes.worker_lock(parent, chosen))
        if isinstance(hold, worker_panes.Kept):
            source = prompt.resolve()
            pending = progress_history.select_dispatch(session_dir, slot=chosen.value, phase=phase, role=role,
                prompt=source, launcher_command=launcher_command,
                work=progress_history.DispatchWork(kind, lens, fix_pass), target=chosen, prompt_source=source)
            if isinstance(pending, pass_recorder.DispatchSelection) and _compaction_retry_pending(session_dir, pending):
                return _pending_compaction_result(session_dir, pending)
            print(hold.blocker, file=sys.stderr)
            return ExitCode.BUSY
        record = staff.snapshot()
        row = worker_admission.find_worker(record, parent, chosen.value)
        if ((row is None and hold.bound) or (row is not None and not hold.owns(row))
                or (selected_row is not None and (row is None or row.id != selected_row.id
                                                 or row.current_session != selected_row.current_session))):
            print(f"{chosen.value}: the worker changed before assignment", file=sys.stderr)
            return ExitCode.BUSY
        availability: CheckAssignmentAvailability | AvailabilityAtUse = AvailabilityAtUse.DIRECT_DISPATCH
        if row is not None:
            paths = runs.by_worker.get(row.id)
            if paths is None:
                paths = _assignment_runs(record, parent, row, additional=(session_dir,)).by_worker.get(row.id)
            if not paths:
                print(f"{row.name}: the worker's assignment folders are unavailable", file=sys.stderr)
                return ExitCode.BUSY
            refreshed = list(paths)
            owner = record.parent_of(row)
            if owner is not None and isinstance(owner.run, staff.OpenRun):
                refreshed.append(owner.run.path)
            linked = progress_history.recorded_dispatch_run(row.id)
            if isinstance(linked, progress_history.RecordedDispatchRun):
                refreshed.append(linked.directory)
            directories = tuple(dict.fromkeys(refreshed))
            current = unit_runs.LauncherRunDirectories({row.id: directories}, runs.by_session)
            status_file = os.environ.get(progress_history.ASSIGN_STATUS_FILE_ENV, "") or completion_actions.status_file
            if status_file:
                status_file = str(progress_history.worker_artifact(Path(status_file), slot, chosen.value))
            availability = CheckAssignmentAvailability(current, status_file)
        if chosen.value != slot:
            if completion_actions.status_file:
                status = Path(completion_actions.status_file)
                target = progress_history.worker_artifact(status, slot, chosen.value)
                completion_actions = replace(completion_actions, status_file=str(target))
                if owner is not None:
                    writer = CompletionArtifacts(owner, session_dir)
                    if writer.protected:
                        writer.rename(status, target)
            if completion_actions.summary_file:
                target = progress_history.worker_artifact(Path(completion_actions.summary_file), slot, chosen.value)
                completion_actions = replace(completion_actions, summary_file=str(target))
            if isinstance(reply, Path):
                reply = progress_history.worker_artifact(reply, slot, chosen.value)
        result = dispatch(parent, chosen, role, prompt, dispatch_id, session_dir=session_dir, slot=chosen.value,
            phase=phase, kind=kind, lens=lens, launcher_command=launcher_command, reply=reply,
            fix_cause=fix_cause, fix_pass=fix_pass, activity=activity, early_ready=early_ready, policy=policy,
            completion_actions=completion_actions, on_submission=cleanup.pop_all, lock=hold,
            new_pass_instance_id=new_pass_instance_id, notices=notices, availability=availability)
        if result is DispatchSubmission.SUBMITTED:
            _ = cleanup.pop_all()
        return result


def _refuse_answer(name: WorkerName, reason: str, code: ExitCode = ExitCode.REFUSED) -> ExitCode:
    print(f"{name.value}: {reason}. Use assign.sh for new work.", file=sys.stderr)
    return code


@dataclass(frozen=True)
class PendingAnswer:
    """Remember an answer until the transcript proves a new input request."""

    session: str
    record_number: int
    request_digest: str
    answer_digest: str
    question_call: str


class AnswerBlocked(Enum):
    PENDING = "an answer is pending for this input request"
    UNAVAILABLE = "the transcript cannot verify the pending answer"


def _answer_digest(text: str) -> str:
    return hashlib.sha256(text.strip().encode()).hexdigest()


def _answer_request(path: Path, row: staff.StaffRow, backend: staff.ClaudeBackend | staff.CodexBackend,
                    status: herdr.AgentStatus, text: str) -> PendingAnswer | AnswerBlocked:
    saved = worker_lifecycle.read_json_object(path)
    session = str(row.current_session)
    boundary = saved.get("record_number", 0)
    if not isinstance(boundary, int) or isinstance(boundary, bool):
        return AnswerBlocked.UNAVAILABLE
    if saved and (saved.get("session") != session or boundary < 1):
        return AnswerBlocked.UNAVAILABLE

    source = worker_admission.transcript_source(row, backend)
    number = 0
    last: dict[str, object] = {}
    question_call = ""
    accepted = False
    next_request = False
    records = worker_admission.transcript_records(
        source, boundary=worker_admission.TranscriptRecordBoundary.COMPLETE_LINES_WITH_TAIL)
    for number, fields in enumerate(records, start=1):
        if isinstance(fields, worker_admission.TornTranscriptRecord):
            if number >= boundary:
                return AnswerBlocked.UNAVAILABLE
            continue
        last = fields
        if number == boundary and _answer_digest(json.dumps(fields, sort_keys=True)) != saved.get("request_digest"):
            return AnswerBlocked.UNAVAILABLE
        raw = fields.get("payload")
        payload = cast(dict[str, object], raw) if isinstance(raw, dict) else {}
        kind = payload.get("type")
        call = payload.get("call_id")
        if kind == "function_call" and payload.get("name") == "request_user_input" and isinstance(call, str):
            if number > boundary and accepted and status is herdr.AgentStatus.BLOCKED and call != saved.get("question_call"):
                next_request = True
            question_call = call
        elif kind == "function_call_output" and question_call and call == question_call:
            if number > boundary and call == saved.get("question_call"):
                accepted = True
            question_call = ""
        if not saved or number <= boundary:
            continue
        entry = worker_admission.transcript_entry(fields, set())
        if entry.startswith("user:\n"):
            body = entry.removeprefix("user:\n")
            accepted |= _answer_digest(script_prompt(body)) == saved.get("answer_digest")
        elif accepted and (entry.startswith("assistant:\n")
                           or fields.get("type") == "event_msg" and kind == "task_complete"
                           or fields.get("type") == "system" and fields.get("subtype") == "turn_duration"):
            next_request = True
    if not number or number < boundary:
        return AnswerBlocked.UNAVAILABLE
    if saved and not next_request:
        return AnswerBlocked.PENDING
    return PendingAnswer(session, number, _answer_digest(json.dumps(last, sort_keys=True)),
                         _answer_digest(script_prompt(text)), question_call if status is herdr.AgentStatus.BLOCKED else "")


def answer(parent: StaffId, name: WorkerName, text: str) -> ExitCode:
    with worker_panes.worker_lock(parent, name) as hold:
        if isinstance(hold, worker_panes.Kept):
            return _busy_dispatch(f"{name.value}: {hold.blocker}. Use assign.sh for new work.")
        record = staff.snapshot()
        row = worker_admission.find_worker(record, parent, name.value)
        if row is None:
            print(f"{name.value}: no worker staff row", file=sys.stderr)
            return ExitCode.REFUSED
        run = worker_panes.worker_run(row, record=record)
        active = progress_history.active_dispatch(row.id, run)
        if isinstance(active, progress_history.ActiveDispatchUnavailable):
            return _refuse_answer(name, "the active pass is unavailable", ExitCode.UNCERTAIN)
        if isinstance(active, progress_history.NoActiveDispatch):
            return _refuse_answer(name, "no active pass")
        passed = worker_panes.worker_pass(row, run, record=record)
        delivery = pass_recorder.dispatch_delivery(passed)
        if passed.get("instance_id") != active.instance_id or not all((delivery.line, delivery.completion_id, delivery.reply_file)):
            return _refuse_answer(name, "the active pass has no saved delivery")

        backend = record.backends.get(row.id, staff.BACKEND_UNAVAILABLE)
        if isinstance(backend, staff.BackendUnavailable):
            return _refuse_answer(name, "the worker transcript is unavailable", ExitCode.UNCERTAIN)
        receipt = dispatch_receipt(row, delivery.line, delivery.completion_id, run=run, require_done=True)
        if isinstance(receipt, DispatchComplete):
            return _refuse_answer(name, "the active pass already has a DONE reply")
        if isinstance(receipt, DispatchEvidenceUnknown):
            return _refuse_answer(name, "the worker transcript is unavailable", ExitCode.UNCERTAIN)
        if isinstance(receipt, DispatchNotSubmitted):
            return _refuse_answer(name, "the transcript does not show acceptance of the active pass")

        state = worker_panes.observe_worker(parent, row, record)
        if not isinstance(state, worker_panes.OpenWorker) or not hold.owns(row):
            return _refuse_answer(name, "the worker pane is unavailable", ExitCode.UNCERTAIN)
        if state.status is herdr.AgentStatus.WORKING:
            return _refuse_answer(name, "the worker is working", ExitCode.BUSY)
        if not state.status.idle and state.status is not herdr.AgentStatus.BLOCKED:
            return _refuse_answer(name, "the worker may be working", ExitCode.UNCERTAIN)
        client = _client()
        if (owner := record.by_id(parent)) is not None:
            try:
                _require_review_delivery(owner, row, run, passed)
                _require_running_review(owner, client, state)
            except (OSError, ValueError, ProductionPathsError, herdr_types.HerdrFailure, subprocess.SubprocessError,
                    staff.StaffRefused, staff.StaffUnreadable) as error:
                return _refuse_answer(name, str(error))
        blocked_codex = state.status is herdr.AgentStatus.BLOCKED and isinstance(backend, staff.CodexBackend)
        submitted = text if blocked_codex else text + "\n" + _done_instruction(delivery.completion_id)
        scope = _answer_digest(json.dumps((row.id, active.instance_id, delivery.completion_id)))
        path = run / f".{scope}.pending-answer.json"
        request = _answer_request(path, row, backend, state.status, submitted)
        if isinstance(request, AnswerBlocked):
            code = ExitCode.BUSY if request is AnswerBlocked.PENDING else ExitCode.UNCERTAIN
            return _refuse_answer(name, request.value, code)
        owner = record.by_id(parent)
        if owner is None:
            return _refuse_answer(name, "the owner is unavailable")
        artifacts = CompletionArtifacts(owner, run)
        artifacts.write(path, json.dumps(asdict(request)))
        try:
            if blocked_codex:
                client.answer_question(state.pane, submitted, target=str(row.current_session))
            else:
                client.prompt(state.pane, submitted, target=str(row.current_session))
        except herdr.HerdrCommandNotStarted:
            artifacts.unlink(path)
            raise
        return ExitCode.OK


def _end_refusal(row: staff.StaffRow, record: staff.StaffSnapshot, reason: str | CompletionRecovery) -> str:
    """Name the next step when an open pass waits on a DONE."""
    text = reason if isinstance(reason, str) else reason.value
    owner = record.parent_of(row)
    if reason is not CompletionRecovery.WAITING_ON_REPLY or owner is None or not isinstance(
            progress_history.active_dispatch(row.id, worker_panes.worker_run(row, record=record)), progress_history.ActiveDispatch):
        return text
    return f"{text}. Run worker.py answer {row.name} --text TEXT from the {owner.name} session to ask the worker for the DONE."


def end(parent: StaffId, name: WorkerName, *,
        lock: worker_panes.WorkerLockHold | worker_panes.WorkerLockAtUse = worker_panes.WorkerLockAtUse.ACQUIRE,
        retire: bool = True) -> ExitCode:
    expected = worker_admission.find_worker(staff.snapshot(), parent, name.value)
    with ExitStack() as operations:
        hold = operations.enter_context(worker_panes.worker_lock(parent, name, blocking=True)) if isinstance(lock, worker_panes.WorkerLockAtUse) else lock
        if isinstance(hold, worker_panes.Kept):
            print(hold.blocker, file=sys.stderr)
            return ExitCode.KEPT
        record = staff.snapshot()
        row = worker_admission.find_worker(record, parent, name.value)
        if (not hold.active or hold.parent != parent or hold.name != name
                or (row is None and (hold.bound or expected is not None))
                or (row is not None and (expected is None or not hold.owns(row)
                    or row.id != expected.id or row.current_session != expected.current_session))):
            print(f"{name.value}: the worker changed before end", file=sys.stderr)
            return ExitCode.KEPT
        if row is None:
            return ExitCode.OK
        director = record.parent_of(row)
        recovery = CompletionRecovery.NO_PENDING_NOTICE
        if retire and director is not None:
            run = worker_panes.worker_run(row, record=record)
            passed = worker_panes.worker_pass(row, run, record=record)
            delivery = pass_recorder.dispatch_delivery(passed)
            completion = progress_history.hook_completion(passed)
            try:
                unfinished = isinstance(completion, progress_history.HookCompletion) and not completion.finished
                canceled_without_hook = (passed.get("status") == "canceled"
                                         and isinstance(completion, progress_history.NoHookCompletion))
                if unfinished or canceled_without_hook or (passed.get("status") in ("active", "refused") and delivery.line and isinstance(
                        dispatch_receipt(row, delivery.line, delivery.completion_id, run=run, require_transcript=True),
                        DispatchComplete)):
                    recovery = _completion_notices().recover_completion(row, director, run, hold, record=record)
                    instance = passed.get("instance_id")
                    delivered_stop = (recovery is CompletionRecovery.WAITING_ON_REPLY
                                      and isinstance(completion, progress_history.HookCompletion)
                                      and completion.outcome == "stopped" and passed.get("status") == "canceled"
                                      and isinstance(instance, str)
                                      and _stopped_notice_settled(run, instance))
                    no_late_reply = canceled_without_hook and recovery is CompletionRecovery.WAITING_ON_REPLY
                    if (recovery not in (CompletionRecovery.NO_PENDING_NOTICE, CompletionRecovery.RECOVERED)
                            and not delivered_stop and not no_late_reply):
                        reason: str | CompletionRecovery = recovery
                        if recovery is CompletionRecovery.PENDING_CLOSE:
                            saved = progress_history.hook_completion(worker_panes.worker_pass(row, run))
                            if isinstance(saved, progress_history.HookCompletion) and saved.details:
                                detail = " ".join(saved.details[-1].splitlines())
                                reason = f"{recovery.value}: {detail}"
                        print(f"{row.name}: {_end_refusal(row, record, reason)}", file=sys.stderr)
                        return ExitCode.KEPT
            except (OSError, ValueError, recorder_state.OldRecorderState, herdr_types.HerdrFailure, subprocess.SubprocessError,
                    staff.StaffRefused, staff.StaffUnreadable) as error:
                print(f"{row.name}: completion recovery failed: {' '.join(str(error).splitlines())}", file=sys.stderr)
                return ExitCode.KEPT
        if recovery is not CompletionRecovery.RECOVERED:
            result = worker_panes.close(parent, name, lock=hold)
            if isinstance(result, worker_panes.Kept):
                print(_end_refusal(row, record, result.reason), file=sys.stderr)
                return ExitCode.KEPT
            if isinstance(result, worker_panes.UncertainWorker):
                return ExitCode.UNCERTAIN
        if retire:
            _ = staff.retire(row.id)
        return ExitCode.OK


def resume_receipt(row: staff.StaffRow, dispatch: progress_history.ActiveDispatch
                   ) -> DispatchNotSubmitted | DispatchSubmitted | DispatchComplete | DispatchEvidenceUnknown | DispatchAwaitingReply:
    """Read acceptance and closure once before choosing the worker's resume step."""
    backend = staff.backend(row)
    if isinstance(backend, staff.BackendUnavailable):
        return DispatchEvidenceUnknown("worker transcript backend is unavailable")
    return transcript_receipt(worker_admission.conversation_entries(row, backend, set()),
        DispatchPrompt(dispatch.instance_id, include_stage_two=True), DoneReply(dispatch.instance_id)).receipt


def resume_step(row: staff.StaffRow, dispatch: progress_history.ActiveDispatch
                ) -> BusyPass | UnsubmittedAssignment | NoOwnPass | DispatchEvidenceUnknown:
    """Use the transcript receipt to select one step for every resume path."""
    receipt = resume_receipt(row, dispatch)
    if isinstance(receipt, DispatchNotSubmitted):
        return UnsubmittedAssignment(dispatch.instance_id, dispatch.launcher_command)
    if isinstance(receipt, (DispatchSubmitted, DispatchAwaitingReply)):
        return BusyPass(dispatch.instance_id)
    return receipt if isinstance(receipt, DispatchEvidenceUnknown) else NO_OWN_PASS


@dataclass(frozen=True)
class NoLaunchCleanup:
    """The restore owns no new pane to close."""


NO_LAUNCH_CLEANUP = NoLaunchCleanup()


@dataclass(frozen=True)
class WorkerResume:
    step: BusyPass | UnsubmittedAssignment | NoOwnPass
    detached: str = ""


def _resume_launcher(row: staff.StaffRow, resume: WorkerResume, *,
                     on_submission: Callable[[], object] | NoLaunchCleanup = NO_LAUNCH_CLEANUP
                     ) -> Literal[ExitCode.OK] | DispatchEvidenceUnknown:
    """Deliver the resume step while restore holds the dispatch lock."""
    record = staff.snapshot()
    parent = record.parent_of(row)
    step, detached = resume.step, resume.detached
    recipient = row if isinstance(step, (BusyPass, NoOwnPass)) else parent
    client = _client()
    if parent is None or recipient is None:
        return DispatchEvidenceUnknown(f"{'worker' if recipient == row else 'director'} session is unavailable")
    pane = next((item for item in client.tab_sessions() if item.session_id == recipient.current_session), None)
    if pane is None:
        return DispatchEvidenceUnknown(f"{'worker' if recipient == row else 'director'} session is unavailable")
    pane_id = pane.pane_id
    if isinstance(step, (BusyPass, NoOwnPass)):
        status = next((agent["agent_status"] for agent in client.agents() if agent["pane_id"] == pane_id),
                      herdr.AgentStatus.UNKNOWN)
        if status not in (herdr.AgentStatus.IDLE, herdr.AgentStatus.DONE):
            return DispatchEvidenceUnknown("The worker must be idle to receive its resume message")
    text = (launcher_prompt(row.id, step.dispatch_id, step.command) + ("\n" + detached if detached else "")
            if isinstance(step, UnsubmittedAssignment) else resume_message("", own_pass=step, detached=detached))
    submit = (client.prompt if isinstance(on_submission, NoLaunchCleanup) else
              partial(client.prompt, on_submission=on_submission))
    submit(pane_id, text, target=str(recipient.current_session))
    return ExitCode.OK


def _detached_resume_context(row: staff.StaffRow, directories: Iterable[Path]) -> str:
    lines: list[str] = []
    for directory in dict.fromkeys(directories):
        runs = directory / worker_lifecycle.DETACHED_RUNS_DIR
        for record in detached_runs.live_runs_for_staff(runs, frozenset({row.id})):
            result = runs / f"{record['run_id']}.json"
            log = record["log"]
            command = shlex.join(["cat", "--", str(result), log])
            lines.append(f"Detached run {record['run_id']} remains live. Result: {result}; log: {log}. "
                         + f"Read it with: `{command}`. Its result follows this worker after the move.")
    return "\n".join(lines)


def _resume_needed(state: worker_panes.OpenWorker | worker_panes.StoppedWorker | worker_panes.ClosedWorker,
                   directories: Iterable[Path], record: staff.StaffSnapshot,
                   ) -> WorkerResume | DispatchEvidenceUnknown | None:
    """Decide from the recorded dispatches, transcript, pane and live runs."""
    directories = tuple(dict.fromkeys(directories))
    detached = _detached_resume_context(state.row, directories)
    active: dict[str, progress_history.ActiveDispatch] = {}
    with staff.supplied_snapshot(record):
        for directory in directories:
            dispatch = progress_history.active_dispatch(state.row.id, directory)
            if isinstance(dispatch, progress_history.ActiveDispatchUnavailable):
                return DispatchEvidenceUnknown(dispatch.reason)
            if isinstance(dispatch, progress_history.ActiveDispatch):
                _ = active.setdefault(dispatch.instance_id, dispatch)
    if len(active) > 1:
        return DispatchEvidenceUnknown("The worker has multiple active dispatches")
    step = resume_step(state.row, next(iter(active.values()))) if active else NO_OWN_PASS
    if isinstance(step, DispatchEvidenceUnknown):
        return step
    if detached or isinstance(step, UnsubmittedAssignment):
        return WorkerResume(step, detached)
    already_working = (isinstance(state, worker_panes.OpenWorker)
                       and state.status in (herdr.AgentStatus.WORKING, herdr.AgentStatus.RUNNING))
    return WorkerResume(step) if isinstance(step, BusyPass) and not already_working else None


def own_pass(row: staff.StaffRow, record: staff.StaffSnapshot, *,
             run: worker_panes.DispatchRun | worker_panes.HolderRun = worker_panes.HolderRun.CURRENT
             ) -> BusyPass | UnsubmittedAssignment | NoOwnPass:
    """Read the worker's active pass from its own run."""
    try:
        parent = record.parent_of(row)
        if parent is None:
            raise ValueError("worker parent is unavailable")
        directory = run.directory if isinstance(run, worker_panes.DispatchRun) else worker_panes.worker_run(row, record=record)
        active = progress_history.active_dispatch(row.id, directory)
        if isinstance(active, progress_history.ActiveDispatchUnavailable):
            print(f"anomaly: {row.name}: {active.reason}", file=sys.stderr)
        elif isinstance(active, progress_history.ActiveDispatch):
            step = resume_step(row, active)
            if not isinstance(step, DispatchEvidenceUnknown):
                return step
            print(f"anomaly: {row.name}: {step.reason}", file=sys.stderr)
    except (OSError, ValueError) as error:
        print(f"anomaly: {row.name}: {error}", file=sys.stderr)
    return NO_OWN_PASS


def resume_workers(parent: StaffId, record: staff.StaffSnapshot) -> ResumeWorkers:
    """Supply pane dispatches and legacy Claude child launchers."""
    from scripts.agents.inventory import worker_resumes

    holder = record.by_id(parent)
    if holder is None:
        return ResumeWorkers()
    try:
        enrolled = isinstance(worker_panes.enrolled_worker_owner(holder), EnrolledPaths)
    except ProductionPathsError as error:
        raise ValueError(str(error)) from error
    legacy_launches = [] if enrolled else worker_resumes(holder, record)
    results = {staff.StaffId(result.staff_id if isinstance(result, Unavailable) else result.seat["staff_id"]): result
               for result in legacy_launches}
    members = record.find(kind="worker", parent=parent)
    unavailable = [row for row in members if isinstance(results.get(row.id), Unavailable)]
    recorded = session_staff.worker_run_entries(record, unavailable) if unavailable else {}
    results = {identity: result for identity, result in results.items()
               if not isinstance(result, Unavailable) or identity in recorded}
    panes: list[PaneWorkerResume] = []
    runs = [Path(run) for run in unit_runs.held(str(holder.current_session),
        os.environ.get("ROSTER_RUN_ROOT"), record=record)]
    scanned = {str(holder.current_session): [str(path) for path in runs]}
    with progress_history.dispatch_reads(record, runs):
        for row in members:
            if enrolled:
                role = worker_panes.dispatch_role(row)
                if not isinstance(role, str) or role not in worker_panes.REVIEW_ROLES:
                    continue
            run = worker_panes.worker_run(row, record=record, scanned=scanned)
            active = progress_history.active_dispatch(row.id, run)
            if isinstance(active, progress_history.ActiveDispatchUnavailable):
                print(f"anomaly: {row.name}: {active.reason}", file=sys.stderr)
                continue
            if isinstance(active, progress_history.ActiveDispatch):
                _ = results.pop(row.id, None)
                if enrolled:
                    try:
                        passed = worker_panes.worker_pass(row, run, record=record)
                        if passed.get("instance_id") != active.instance_id:
                            raise ValueError("the saved review pass changed")
                        _require_review_delivery(holder, row, run, passed)
                    except (OSError, ValueError, ProductionPathsError) as error:
                        print(f"anomaly: {row.name}: {error}", file=sys.stderr)
                        continue
                step = resume_step(row, active)
                if isinstance(step, (BusyPass, UnsubmittedAssignment)):
                    panes.append(PaneWorkerResume(row.id, step))
                elif isinstance(step, DispatchEvidenceUnknown):
                    print(f"anomaly: {row.name}: {step.reason}", file=sys.stderr)
            elif row.id not in results:
                latest = progress_history.last_dispatch(row.id)
                if (isinstance(latest, progress_history.RecordedDispatch)
                        and isinstance(latest.status, str) and latest.status != "active"):
                    continue
                panes.append(PaneWorkerResume(row.id, IdleWorker()))
    return ResumeWorkers(list(results.values()), str(worker_panes.worker_directory(record, parent)) if results else "", panes)


@dataclass(frozen=True)
class PaneWorkerStates:
    states: list[worker_panes.WorkerPane]
    runs: list[Path]


def _read_workers(parent: staff.StaffRow, record: staff.StaffSnapshot, *,
                  only: frozenset[StaffId] | worker_panes.SelectedWorkerRows | Literal["all"] = "all",
                  scanned: dict[str, list[str]] | None = None) -> PaneWorkerStates:
    runs = unit_runs.held(str(parent.current_session), os.environ.get("ROSTER_RUN_ROOT"), scanned=scanned, record=record)
    if only == "all":
        states = worker_panes.observe(parent.id, record)
    elif isinstance(only, worker_panes.SelectedWorkerRows):
        states = worker_panes.observe_parents([parent], record, selection=only)[parent.id]
    else:
        names = [WorkerName.parse(row.name) for row in record.find(kind="worker", parent=parent.id) if row.id in only]
        states = worker_panes.observe_selected(parent.id, record, names)
    return PaneWorkerStates(states, [Path(run) for run in runs])


@dataclass(frozen=True)
class NotRestored:
    """Restore never starts this worker, so its id leaves the restart record."""

    reason: str


def not_restored(row: staff.StaffRow, record: staff.StaffSnapshot, *,
                 runs: dict[str, list[str]] | None = None) -> NotRestored | None:
    """Route every worker to restore before restart removes its saved entry."""
    del row, record, runs
    return None


def _restore_worker_pane(parent: StaffId, row: staff.StaffRow, hold: worker_panes.WorkerLockHold, *,
                         selection: worker_panes.SelectedWorkerRows,
                         account: worker_panes.ConfiguredWorkerAccount | worker_panes.DestinationWorkerAccount
                                  | accounts.AccountSelection = worker_panes.ConfiguredWorkerAccount.CURRENT,
                         runs: dict[str, list[str]] | None = None,
                         codex: herdr.CodexConversations | herdr.CodexLaunchRead = herdr.CodexLaunchRead.CURRENT,
                         policy: worker_panes.UseRolePolicy | worker_panes.LaunchPolicy = worker_panes.UseRolePolicy.CURRENT,
                         ) -> worker_panes.WorkerStart:
    return worker_panes.start_or_resume(parent, WorkerName.parse(row.name), worker_panes.dispatch_role(row),
                                        account=account, lock=hold, runs=runs, codex=codex, policy=policy,
                                        selection=selection)


@dataclass(frozen=True)
class ParkedRestoreRequest:
    """Apply a restore account and report panes that its reap opens."""

    workers: frozenset[StaffId]
    account: worker_panes.ConfiguredWorkerAccount | worker_panes.DestinationWorkerAccount | accounts.AccountSelection
    started_panes: dict[StaffId, str]
    codex: herdr.CodexConversations | herdr.CodexLaunchRead


class RestoreFromRunEvent(Enum):
    CURRENT = "use the configured restore account"


_parked_restore_request: ContextVar[ParkedRestoreRequest | RestoreFromRunEvent] = ContextVar(
    "parked_restore_request", default=RestoreFromRunEvent.CURRENT)


def restore_parked_worker(row: staff.StaffRow, parent: StaffId, hold: worker_panes.WorkerLockHold) -> bool:
    """Restore the pane for a result without repeating its accepted assignment."""
    record = staff.snapshot()
    current = record.by_id(row.id)
    holder = record.by_id(parent)
    if (current is None or holder is None or hold.parent != parent or not hold.owns(current)
            or current.name != hold.name.value or current.current_session != row.current_session):
        return False
    herdr.refresh_restore_pane_evidence()
    selection = worker_panes.SelectedWorkerRows(frozenset({current.id}))
    observed = worker_panes.observe_parents([holder], record, selection=selection)
    state = next(iter(observed[parent]), None)
    if isinstance(state, worker_panes.OpenWorker):
        return True
    if not isinstance(state, (worker_panes.ClosedWorker, worker_panes.StoppedWorker)):
        return False
    row = state.row
    run = worker_panes.worker_run(row, record=record)
    policy = worker_panes.parked_launch_policy(row, worker_panes.worker_pass(row, run, record=record))
    if isinstance(policy, progress_history.ParkedLaunchUnavailable):
        return False
    if quota_state.no_worker_mode() and policy.settings.kind == "codex":
        return False
    request = _parked_restore_request.get()
    if not isinstance(request, ParkedRestoreRequest) or row.id not in request.workers:
        result = _restore_worker_pane(parent, row, hold, selection=selection, policy=policy)
    else:
        result = _restore_worker_pane(parent, row, hold, selection=selection, policy=policy,
                                      account=request.account, codex=request.codex)
        if isinstance(result, worker_panes.OpenWorker):
            request.started_panes[row.id] = result.pane
    return startup_result(WorkerName.parse(row.name), result) == ExitCode.OK


def park_waiting_worker(row: staff.StaffRow, hold: worker_panes.WorkerLockHold, park: worker_panes.ParkWaitingPass,
                        delivery: pass_recorder.DispatchDelivery, receipt: DispatchWaitingForRuns
                        ) -> worker_panes.Closed | worker_panes.Kept | worker_panes.UncertainWorker:
    with detached_runs.guard_worker_park(park.run, receipt.runs) as guarded:
        if guarded and dispatch_receipt(row, delivery.line, park.stage, run=park.run, require_transcript=True) == receipt:
            return worker_panes.close(hold.parent, hold.name, lock=hold, park=park)
    return worker_panes.Kept("the detached run wait changed before parking")


def _accepted_assignment(row: staff.StaffRow, passed: dict[str, object]) -> bool:
    delivery = pass_recorder.dispatch_delivery(passed)
    if not delivery.line or not delivery.completion_id:
        return False
    receipt = _dispatch_transcript(row, delivery.line, DoneReply(delivery.completion_id),
                                   require_transcript=True, exact_prompt=True).receipt
    return isinstance(receipt, (DispatchSubmitted, DispatchAwaitingReply, DispatchComplete))


def _accepted_result_assignment(row: staff.StaffRow, passed: dict[str, object],
                                evidence: detached_runs.WorkerResultReceipt) -> bool:
    return (evidence.park_acceptance is detached_runs.ParkedAssignmentAcceptance.VERIFIED
            or _accepted_assignment(row, passed))


def clear_result_park(row: staff.StaffRow, park: worker_panes.ParkWaitingPass) -> bool:
    """Clear the matching marker or check the pass after a failed comparison."""
    if progress_history.record_worker_park(park.run, row.id, park.instance, park.stage,
                                          launch=progress_history.ClearWorkerPark.MARKER):
        return True
    current = worker_panes.worker_pass(row, park.run)
    return (current.get("instance_id") == park.instance
            and pass_recorder.dispatch_delivery(current).completion_id == park.stage
            and not progress_history.parked_worker_pass(current))


class RestoreFailureAction(Enum):
    RETRY = "retry restore"
    INSPECT_WORKER = "The director must inspect the worker."


@dataclass
class RestoreReport:
    restored: set[StaffId] = field(default_factory=set)
    # A restored worker can still need a step to recover its reply.
    failures: dict[StaffId, tuple[str, str]] = field(default_factory=dict)
    run_failures: dict[Path, str] = field(default_factory=dict)
    skipped: dict[StaffId, str] = field(default_factory=dict)
    record_busy: set[StaffId] = field(default_factory=set)
    failure_actions: dict[StaffId, RestoreFailureAction] = field(default_factory=dict)


@herdr.process_action()
@herdr.restore_pane_evidence_run()
def restore(parent: StaffId, *, record: staff.StaffSnapshot | None = None,
            only: frozenset[StaffId] | Literal["all"] = "all", account: str | accounts.AccountSelection = "",
            started_panes: dict[StaffId, str] | Literal["discard"] = "discard",
            runs: dict[str, list[str]] | None = None,
            codex: herdr.CodexConversations | herdr.CodexLaunchRead = herdr.CodexLaunchRead.CURRENT,
            report: RestoreReport | None = None) -> ExitCode:
    completion_notices = _completion_notices()
    record = staff.snapshot() if record is None else record
    holder = next(row for row in record.rows if row.id == parent)
    changed = False
    outcome = ExitCode.OK
    launch_account = ((worker_panes.DestinationWorkerAccount(account) if account else
                       worker_panes.ConfiguredWorkerAccount.CURRENT) if isinstance(account, str) else account)
    observed = _read_workers(holder, record, only=only, scanned=runs)
    states = observed.states
    workers_held = quota_state.no_worker_mode()
    held_runs = list(dict.fromkeys(path.resolve() for path in observed.runs))
    selected_workers = tuple(dict.fromkeys(state.row.name for state in states
        if state.row.id in only and isinstance(state, (worker_panes.OpenWorker,
            worker_panes.StoppedWorker, worker_panes.ClosedWorker)))) if only != "all" else ()
    reaped_panes: dict[StaffId, str] = {}
    request = ParkedRestoreRequest(
        frozenset(state.row.id for state in states), launch_account, reaped_panes, codex)
    token = _parked_restore_request.set(request)
    try:
        for run in held_runs:
            for name in selected_workers if only != "all" else ("",):
                try:
                    reaped = (detached_runs.reap_orphans(str(run), name) if name else
                              detached_runs.reap_orphans(str(run)))
                    if report is None:
                        for run_id in reaped:
                            print(f"{holder.name}: reaped detached run {run_id}")
                except OSError as error:
                    if report is None:
                        print(f"anomaly: {run}: {error}", file=sys.stderr)
                    else:
                        report.run_failures[run] = str(error)
                    outcome = ExitCode.UNCERTAIN
    finally:
        _parked_restore_request.reset(token)
    if isinstance(started_panes, dict):
        started_panes.update(reaped_panes)
    changed = bool(reaped_panes)
    runs = runs if runs is not None else {str(holder.current_session): [str(path) for path in observed.runs]}
    pending_completion = (CompletionRecovery.PENDING_CLOSE, CompletionRecovery.WAITING_ON_RUNS,
                          CompletionRecovery.WAITING_ON_REPLY)

    def failed(row: staff.StaffRow, reason: str, code: ExitCode = ExitCode.UNCERTAIN, *,
               notice: str = "", action: RestoreFailureAction = RestoreFailureAction.RETRY) -> None:
        nonlocal outcome
        outcome = code
        if action is RestoreFailureAction.INSPECT_WORKER:
            reason = f"{reason.rstrip('.')}. {action.value}"
        if report is not None:
            report.failures[row.id] = row.name, reason
            report.failure_actions[row.id] = action
        elif action is RestoreFailureAction.INSPECT_WORKER:
            print(f"{row.name}: {reason}", file=sys.stderr)
        elif notice:
            print(notice, file=sys.stderr)

    def read_locked_pane(current: staff.StaffRow, current_holder: staff.StaffRow, record: staff.StaffSnapshot,
                         selection: frozenset[StaffId] | worker_panes.SelectedWorkerRows | Literal["all"]
                         ) -> worker_panes.WorkerPane | worker_panes.WorkerNotObserved:
        herdr.refresh_restore_pane_evidence()
        refreshed = _read_workers(current_holder, record, only=selection, scanned=runs)
        return next((pane for pane in refreshed.states if pane.row.id == current.id),
                    worker_panes.WorkerNotObserved.CURRENT)

    with progress_history.dispatch_reads(record, observed.runs):
        for previous in states:
            state = previous
            name = previous.row.name
            if isinstance(previous, (worker_panes.ProcessUnknownWorker, worker_panes.UncertainWorker)):
                failed(previous.row, previous.reason)
                continue
            resume_sent = False
            try:
                worker_name = WorkerName.parse(name)
                with worker_panes.worker_lock(parent, worker_name) as hold, ExitStack() as cleanup:
                    if isinstance(hold, worker_panes.Kept):
                        failed(state.row, hold.blocker, ExitCode.REFUSED)
                        continue
                    current_record = staff.authority_snapshot()
                    current = worker_admission.find_worker(current_record, parent, worker_name.value)
                    current_holder = current_record.by_id(parent)
                    if (current is None or current.id != previous.row.id or not hold.owns(current)
                            or current.current_session != previous.row.current_session or current_holder is None):
                        failed(previous.row, "The worker changed before restore")
                        continue
                    run = worker_panes.worker_run(current, record=current_record, scanned=runs)
                    passed = worker_panes.worker_pass(current, run, record=current_record)
                    parked = progress_history.parked_worker_pass(passed)
                    selected_ids = frozenset({current.id})
                    selected_row = worker_panes.SelectedWorkerRows(selected_ids)
                    selection = selected_row if only == "all" or parked else "by_name"
                    observed_rows = "all" if only == "all" else selected_row if parked else selected_ids
                    refreshed_state = read_locked_pane(current, current_holder, current_record, observed_rows)
                    if isinstance(refreshed_state, worker_panes.WorkerNotObserved):
                        failed(previous.row, "The worker was not observed during restore")
                        continue
                    state = refreshed_state
                    if isinstance(state, (worker_panes.ProcessUnknownWorker, worker_panes.UncertainWorker)):
                        failed(state.row, state.reason)
                        continue
                    if isinstance(worker_panes.enrolled_worker_owner(current_holder), EnrolledPaths):
                        _require_review_delivery(current_holder, current, run, passed)
                        if isinstance(state, worker_panes.OpenWorker):
                            _require_running_review(current_holder, _client(), state)
                    evidence = detached_runs.restore_result_evidence(run, state, passed, current_record)
                    if isinstance(evidence, detached_runs.UncertainResultDelivery):
                        failed(state.row, evidence.reason, action=RestoreFailureAction.INSPECT_WORKER)
                        continue
                    if (isinstance(evidence, detached_runs.WorkerResultReceipt)
                            and not _accepted_result_assignment(state.row, passed, evidence)):
                        failed(state.row, "The result receipt does not prove assignment acceptance",
                               notice=f"anomaly: {name}: the result receipt does not prove assignment acceptance")
                        continue
                    # A result delivery can reopen the pane before restore checks the transcript.
                    recover_first = (parked or passed.get("status") == "canceled" or state.row.id in reaped_panes
                                     or isinstance(evidence, detached_runs.WorkerResultReceipt))
                    plan = None if recover_first else _resume_needed(state, (run, *held_runs), current_record)
                    recovery = CompletionRecovery.NO_PENDING_NOTICE
                    if recover_first or plan is None:
                        recovery = completion_notices.recover_completion(state.row, current_holder, run, hold,
                                                                         record=current_record)
                    if recovery is CompletionRecovery.RECOVERED:
                        if isinstance(report, RestoreReport):
                            report.skipped[state.row.id] = "The worker finished its dispatch"
                        changed = True
                        continue
                    if recovery is CompletionRecovery.WAITING_ON_REAP:
                        failed(state.row, recovery.value, notice=f"{name}: {recovery.value}")
                        continue
                    if parked or state.row.id in reaped_panes or isinstance(evidence, detached_runs.WorkerResultReceipt):
                        fresh = read_locked_pane(current, current_holder, current_record, observed_rows)
                        if not isinstance(fresh, (worker_panes.OpenWorker, worker_panes.StoppedWorker,
                                                  worker_panes.ClosedWorker)):
                            reason = (fresh.value if isinstance(fresh, worker_panes.WorkerNotObserved) else fresh.reason)
                            failed(state.row, reason, notice=f"anomaly: {name}: {reason}")
                            continue
                        state = fresh
                    if isinstance(evidence, detached_runs.WorkerResultReceipt):
                        if recovery in (CompletionRecovery.PENDING_CLOSE, CompletionRecovery.WAITING_ON_RUNS):
                            failed(state.row, recovery.value, notice=f"{name}: {recovery.value}")
                            continue
                        passed = worker_panes.worker_pass(current, run, record=current_record)
                        fresh_evidence = detached_runs.restore_result_evidence(run, state, passed, current_record)
                        if isinstance(fresh_evidence, detached_runs.UncertainResultDelivery):
                            failed(state.row, fresh_evidence.reason, action=RestoreFailureAction.INSPECT_WORKER)
                            continue
                        if (fresh_evidence != evidence or not isinstance(evidence.assignment, detached_runs.ResultSubmissionPass)
                                or not _accepted_result_assignment(state.row, passed, evidence)):
                            failed(state.row, "The result or assignment evidence changed during restore")
                            continue
                        if not isinstance(state, worker_panes.OpenWorker) or state.status not in (
                                herdr.AgentStatus.WORKING, herdr.AgentStatus.RUNNING):
                            failed(state.row, "The result arrived", action=RestoreFailureAction.INSPECT_WORKER)
                            continue
                        if progress_history.parked_worker_pass(passed):
                            park = worker_panes.ParkWaitingPass(run, evidence.assignment.instance, evidence.assignment.completion)
                            if not clear_result_park(state.row, park):
                                failed(state.row, "The worker pass changed before park clearance")
                                continue
                        if isinstance(report, RestoreReport):
                            report.restored.add(state.row.id)
                        continue
                    if recovery is not CompletionRecovery.PENDING_CLOSE and parked:
                        if isinstance(state, worker_panes.ClosedWorker):
                            if isinstance(report, RestoreReport):
                                report.skipped[state.row.id] = "The worker remains parked for its result"
                            continue
                        delivery = pass_recorder.dispatch_delivery(passed)
                        park = worker_panes.ParkWaitingPass(run, str(passed["instance_id"]), delivery.completion_id)
                        if isinstance(state, worker_panes.OpenWorker) and state.status.idle:
                            receipt = dispatch_receipt(state.row, delivery.line, park.stage, run=run, require_transcript=True)
                            if isinstance(receipt, DispatchWaitingForRuns) and receipt.can_park:
                                closed = park_waiting_worker(state.row, hold, park, delivery, receipt)
                                if isinstance(closed, worker_panes.Closed):
                                    changed = True
                                elif isinstance(closed, worker_panes.UncertainWorker):
                                    failed(state.row, closed.reason, notice=f"anomaly: {name}: {closed.reason}")
                                continue
                        if not clear_result_park(state.row, park):
                            failed(state.row, "The worker pass changed before park clearance")
                        elif isinstance(report, RestoreReport):
                            report.skipped[state.row.id] = "The park marker cleared for the next worker turn"
                        continue
                    if recovery is CompletionRecovery.WAITING_ON_REPLY:
                        if isinstance(state, worker_panes.ClosedWorker):
                            fresh = read_locked_pane(current, current_holder, current_record, observed_rows)
                            if not isinstance(fresh, (worker_panes.OpenWorker, worker_panes.StoppedWorker,
                                                      worker_panes.ClosedWorker)):
                                failed(state.row, "The worker pane is unavailable after recovery")
                                continue
                            state = fresh
                            if isinstance(state, worker_panes.OpenWorker) and not state.status.idle:
                                changed = True
                                if isinstance(report, RestoreReport):
                                    report.restored.add(state.row.id)
                                continue
                        if passed.get("status") == "canceled":
                            if isinstance(state, worker_panes.ClosedWorker):
                                continue
                            allowed = worker_panes.can_close(state, run=worker_panes.DispatchRun(run), lock=hold)
                            if isinstance(allowed, worker_panes.CloseAllowed):
                                closed = worker_panes.close(parent, WorkerName.parse(state.row.name), lock=hold)
                                if isinstance(closed, worker_panes.Closed):
                                    changed = True
                                    continue
                    if recovery in pending_completion:
                        failed(state.row, recovery.value, notice=f"{name}: {recovery.value}")
                        continue
                    if recover_first:
                        plan = _resume_needed(state, (run, *held_runs), current_record)
                    if isinstance(plan, DispatchEvidenceUnknown):
                        failed(state.row, plan.reason, notice=f"anomaly: {name}: {plan.reason}")
                        continue
                    if plan is None:
                        if isinstance(state, worker_panes.OpenWorker) and not state.status.idle:
                            if isinstance(report, RestoreReport):
                                if state.status in (herdr.AgentStatus.WORKING, herdr.AgentStatus.RUNNING):
                                    report.restored.add(state.row.id)
                                else:
                                    report.skipped[state.row.id] = f"The worker pane reports {state.status.value}"
                            continue
                        if isinstance(state, (worker_panes.OpenWorker, worker_panes.StoppedWorker)):
                            allowed = worker_panes.can_close(state, run=worker_panes.DispatchRun(run), lock=hold)
                            if isinstance(allowed, worker_panes.Kept):
                                if isinstance(report, RestoreReport):
                                    if isinstance(state, worker_panes.OpenWorker) and allowed.blocker == worker_panes.NO_FINAL_REPLY:
                                        report.restored.add(state.row.id)
                                    else:
                                        failed(state.row, allowed.blocker)
                                continue
                            closed = worker_panes.close(parent, WorkerName.parse(name), lock=hold)
                            if not isinstance(closed, worker_panes.Closed):
                                failed(state.row, closed.blocker if isinstance(closed, worker_panes.Kept) else closed.reason)
                                continue
                            changed = True
                        if only != "all" and report is None:
                            print(f"{name}: closed; its next assign resumes it")
                        if isinstance(report, RestoreReport):
                            report.skipped[state.row.id] = "The worker has no active dispatch"
                        continue
                    if (workers_held and not isinstance(state, worker_panes.OpenWorker)
                            and isinstance(staff.backend(state.row), staff.CodexBackend)):
                        if plan.detached:
                            failed(state.row, "No-worker mode keeps this worker stopped",
                                   notice=f"{name}: not resumed: no-worker mode")
                        elif report is None:
                            print(f"{name}: not resumed: no-worker mode")
                        else:
                            report.skipped[state.row.id] = "No-worker mode keeps this worker stopped"
                        continue
                    resume = _resume_launcher
                    if not isinstance(state, worker_panes.OpenWorker):
                        before = rows_before_launch()
                        result = worker_panes.start_or_resume(parent, worker_name, worker_panes.dispatch_role(state.row),
                            account=launch_account, lock=hold, runs=runs, codex=codex, selection=selection)
                        arm_launch_cleanup(cleanup, before, result)
                        if not isinstance(result, worker_panes.OpenWorker):
                            code, reason = _startup_failure(result)
                            if report is None:
                                _ = startup_result(WorkerName.parse(name), result)
                            failed(state.row, reason, code)
                            continue
                        if isinstance(started_panes, dict):
                            started_panes[state.row.id] = result.pane
                        if isinstance(result.created_pane, worker_panes.CreatedWorkerPane):
                            resume = partial(resume, on_submission=cleanup.pop_all)
                        changed = True
                        state = result
                    row = state.row
                    resumed_record = staff.authority_snapshot() if row != current else current_record
                    with staff.supplied_snapshot(resumed_record):
                        resumed = resume(row, plan)
                    if isinstance(resumed, DispatchEvidenceUnknown):
                        failed(row, resumed.reason, notice=f"anomaly: {name}: {resumed.reason}")
                        continue
                    resume_sent = True
                    _ = cleanup.pop_all()
                    # Only the report counts restored workers; it counts one after the worker accepts the assignment.
                    if isinstance(plan.step, UnsubmittedAssignment) and isinstance(report, RestoreReport):
                        dispatch_id = plan.step.dispatch_id
                        assignments = (worker_panes.worker_pass(row, directory, record=resumed_record)
                                       for directory in dict.fromkeys((run, *held_runs)))
                        if not any(item.get("instance_id") == dispatch_id and _accepted_assignment(row, item)
                                   for item in assignments):
                            report.skipped[row.id] = "The director received the launcher instruction"
                            continue
                    if isinstance(report, RestoreReport):
                        report.restored.add(row.id)
                    recovery = completion_notices.recover_completion(row, current_holder, run, hold,
                        record=staff.authority_snapshot(), pane=KeepCompletedPane.KEEP)
                    if report is None and recovery in (*pending_completion, CompletionRecovery.WAITING_ON_REAP):
                        print(f"{name}: {recovery.value}", file=sys.stderr)
            except (OSError, ValueError, ProductionPathsError, herdr_types.HerdrFailure, subprocess.SubprocessError,
                    staff.StaffRefused, staff.StaffUnreadable) as error:
                reason = " ".join(str(error).splitlines()) or type(error).__name__
                if isinstance(error, shutdown_record.RecordBusy) and isinstance(report, RestoreReport):
                    report.record_busy.add(state.row.id)
                if not resume_sent:
                    failed(state.row, reason, notice=f"anomaly: {name}: {reason}")
                elif isinstance(report, RestoreReport):
                    report.failures[state.row.id] = name, f"Could not finish restore. {reason}"
                else:
                    print(f"anomaly: {name}: {reason}", file=sys.stderr)
    if report is None:
        if outcome == ExitCode.OK:
            print(f"{holder.name}: workers {'restored' if changed else 'unchanged'}")
        for result in _finished_worker_results(holder, record, held_runs):
            print(f"{result.worker}: phase {result.phase}; dispatch {result.dispatch_id}; reply {result.reply_path or 'unavailable'}")
    return outcome


def _pass_records(state: dict[str, object]) -> list[dict[str, object]]:
    raw = state.get("pass", {})
    if not isinstance(raw, dict):
        return []
    slots = cast(dict[str, object], raw)
    return ([slots] if "instance_id" in slots else
            [cast(dict[str, object], value) for value in slots.values() if isinstance(value, dict)])


def _dispatch_fields(run: str, entry: dict[str, object]) -> dict[str, object]:
    """Read dispatch evidence from a pass or its recorder event."""
    completion = progress_history.hook_completion(entry)
    reply = (completion.reply_file if isinstance(completion, progress_history.HookCompletion) else
             pass_recorder.dispatch_delivery(entry).reply_file)
    if not reply:
        saved_reply = entry.get("reply_file")
        reply = saved_reply if isinstance(saved_reply, str) else ""
    if not reply:
        command = entry.get("launcher_command")
        if isinstance(command, list):
            parts = cast(list[object], command)
            for index, part in enumerate(parts[:-1]):
                if part in ("--reply-file", "--reply") and isinstance(parts[index + 1], str):
                    reply = cast(str, parts[index + 1])
    return {"dispatch_id": entry.get("instance_id", entry.get("pass_instance_id", "")),
            "run_path": run,
            "completion_state": entry.get("status", "active" if entry.get("event_type") == "pass_started" else "unknown"),
            "reply_path": reply}


@dataclass(frozen=True)
class FinishedWorkerResult:
    worker: str
    phase: str
    dispatch_id: str
    reply_path: str
    finished_at: float


def _finished_worker_results(holder: staff.StaffRow, record: staff.StaffSnapshot,
                             runs: Iterable[Path]) -> list[FinishedWorkerResult]:
    """List completed passes in open phases without changing the recorder."""
    wanted = {str(row.id).encode(): row for row in record.find(kind="worker", parent=holder.id)}
    results: dict[tuple[Path, str], FinishedWorkerResult] = {}
    for run in runs:
        try:
            state = worker_lifecycle.read_json_object(run / unit_runs.STATE_FILE)
        except worker_lifecycle.WorkerStateUnreadable as error:
            print(f"anomaly: {run}: {error}", file=sys.stderr)
            continue
        if unit_runs.run_owner(state, record) != holder:
            continue
        recorder_state.require_current_shape(run / unit_runs.STATE_FILE, state)
        open_instances = {str(phase["id"]): str(phase.get("instance_id", ""))
                          for phase in recorder_state.open_phases(state) if isinstance(phase.get("id"), str)}
        if not open_instances:
            continue
        history = state.get("history_file")
        entries = [*(_worker_history_events(Path(history), wanted, complete=True)
                     if isinstance(history, str) and history else ()), *_pass_records(state)]
        for entry in entries:
            if entry.get("run_id") and state.get("run_id") and entry["run_id"] != state["run_id"]:
                continue
            identity = entry.get("worker_id")
            row = wanted.get(identity.encode()) if isinstance(identity, str) else None
            if row is None or entry.get("status") != "completed":
                continue
            phase = entry.get("phase")
            phase_id = (cast(dict[str, object], phase).get("id") if isinstance(phase, dict)
                        else entry.get("phase_id"))
            phase_instance = (cast(dict[str, object], phase).get("instance_id") if isinstance(phase, dict)
                              else entry.get("phase_instance_id"))
            if (isinstance(phase_id, str) and phase_instance and open_instances.get(phase_id)
                    and phase_instance != open_instances[phase_id]):
                continue
            fields = _dispatch_fields(str(run), entry)
            dispatch_id = fields["dispatch_id"]
            if not isinstance(phase_id, str) or phase_id not in open_instances or not isinstance(dispatch_id, str) or not dispatch_id:
                continue
            stamp = entry.get("finished_at", entry.get("timestamp_epoch", 0))
            results[run, dispatch_id] = FinishedWorkerResult(row.name, phase_id, dispatch_id,
                str(fields["reply_path"]), float(stamp) if isinstance(stamp, (int, float)) else 0)
    return sorted(results.values(), key=lambda result: result.finished_at, reverse=True)


def _worker_list_row(row: staff.StaffRow, facts: dict[str, object]) -> dict[str, object]:
    fields: dict[str, object] = {"name": row.name, "id": str(row.id), "current_session": str(row.current_session), **facts}
    fields["role"] = (row.role.value if isinstance(row.role, staff.KnownWorkerRole)
                      else {"state": "WorkerRoleUnavailable", "reason": "dispatch role is unavailable"})
    if isinstance(row.home, staff.HomeUnknown):
        fields["account"] = {"state": "AccountUnavailable", "reason": "worker home is unavailable"}
    else:
        fields.update(account=accounts.claude_config_label(row.home.path), home=str(row.home.path))
    return fields


@dataclass(frozen=True)
class WorkerDispatchRecord:
    fields: dict[str, object]
    timestamp: float


WORKER_HISTORY_BYTES = 64 * 1024
type WorkerHistoryTargets = dict[bytes, staff.StaffRow]


def _worker_history_events(path: Path, wanted: WorkerHistoryTargets, *,
                           complete: bool = False) -> Generator[dict[str, object]]:
    identity_pattern = re.compile(rb'"worker_id"\s*:\s*"([^"\\]*)"')
    bad_line_reported = False
    try:
        with path.open("rb") as history:
            offset = 0 if complete else max(0, history.seek(0, os.SEEK_END) - WORKER_HISTORY_BYTES)
            _ = history.seek(offset - 1 if offset else 0)
            tail = history.read() if complete else history.read(WORKER_HISTORY_BYTES + bool(offset))
        if offset:
            boundary, tail = tail[:1], tail[1:]
            if boundary != b"\n":
                tail = tail.partition(b"\n")[2]
        for line in reversed(tail.split(b"\n")):
            if b'"pass_started"' not in line and b'"pass_finished"' not in line:
                continue
            identity = identity_pattern.search(line)
            if identity is None or not identity[1] or identity[1] not in wanted:
                continue
            try:
                value = cast(object, json.loads(line))
            except ValueError as error:
                if not bad_line_reported:
                    print(f"anomaly: {path}: {error}", file=sys.stderr)
                    bad_line_reported = True
                continue
            if isinstance(value, dict):
                entry = cast(dict[str, object], value)
                if entry.get("event_type") in ("pass_started", "pass_finished"):
                    yield entry
    except FileNotFoundError:
        pass
    except OSError as error:
        print(f"anomaly: {path}: {error}", file=sys.stderr)


@dataclass(frozen=True)
class RecordedWorkerDispatches:
    by_worker: dict[StaffId, WorkerDispatchRecord]
    by_session: dict[str, list[str]]


def _recorded_worker_dispatches(record: staff.StaffSnapshot, *,
                                 runs: Iterable[Path] | Literal["discover"] = "discover",
                                 read: Callable[[Path], dict[str, object]] = worker_panes.run_state) -> RecordedWorkerDispatches:
    workers = record.find(kind="worker")
    parents = {parent.id: parent for row in workers if (parent := record.parent_of(row)) is not None}
    if runs == "discover":
        runs = [run for root in (os.environ.get("ROSTER_RUN_ROOT"), *worker_panes.parent_run_roots())
                for run in unit_runs.run_folders(root)]
        for parent in parents.values():
            runs.append(unit_runs.state_dir() / "staff/workers" / parent.id)
            if isinstance(parent.run, staff.OpenRun):
                runs.append(parent.run.path)
        if current := unit_runs.environ("RUN_DIR"):
            runs.append(Path(current))
    run_paths = tuple(dict.fromkeys(path.resolve() for path in runs))
    found: dict[StaffId, WorkerDispatchRecord] = {}
    histories: dict[Path, dict[Path, WorkerHistoryTargets]] = {}
    run_ids: dict[Path, object] = {}

    def keep(row: staff.StaffRow, entry: dict[str, object], run: str) -> None:
        stamp = entry.get("finished_at", entry.get("timestamp_epoch", entry.get("started_at", 0)))
        timestamp = float(stamp) if isinstance(stamp, (int, float)) else 0
        previous = found.get(row.id)
        if previous is None or timestamp > previous.timestamp:
            found[row.id] = WorkerDispatchRecord({
                **_dispatch_fields(run, entry),
                "kind": entry.get("kind", entry.get("pass_kind", "")),
                "activity": entry.get("activity", entry.get("pass_activity", "")),
                "called_agent": entry.get("called_agent", {}),
                "status": entry.get("status", "active" if entry.get("event_type") == "pass_started" else "unknown")},
                timestamp)

    by_session: dict[str, list[str]] = {}
    for run in run_paths:
        try:
            state = read(run)
        except (OSError, ValueError) as error:
            unit_runs.report_anomaly(f"anomaly: {error}")
            continue
        try:
            owner = unit_runs.run_owner(state, record)
        except staff.StaffUnreadable as error:
            unit_runs.report_anomaly(f"anomaly: {run}: {error}")
            continue
        run_ids[run] = state.get("run_id")
        parent_id = owner.id if isinstance(owner, staff.StaffRow) else StaffId("")
        if isinstance(owner, staff.StaffRow):
            by_session.setdefault(owner.current_session, []).append(str(run))
        if parent_id not in parents:
            continue
        recorder_state.require_current_shape(run / recorder_state.STATE_FILENAME, state)
        history = state.get("history_file")
        if isinstance(history, str) and history:
            histories.setdefault(Path(history).resolve(), {})[run] = {str(row.id).encode(): row
                for row in workers if row.parent == staff.ReportsTo(parent_id)}
        entries = _pass_records(state)
        for entry in entries:
            identity = entry.get("worker_id")
            row = record.by_id(StaffId(identity)) if isinstance(identity, str) and identity else None
            if row is None or row.kind != "worker" or row.parent != staff.ReportsTo(parent_id):
                continue
            keep(row, entry, str(run))
    for path, sources in histories.items():
        pending = {identity: row for targets in sources.values() for identity, row in targets.items()}
        for entry in _worker_history_events(path, pending):
            identity = entry.get("worker_id")
            target = pending.get(identity.encode()) if isinstance(identity, str) and identity else None
            if target is not None:
                candidates = [run for run, targets in sources.items() if str(target.id).encode() in targets]
                if identity := entry.get("run_id"):
                    candidates = [run for run in candidates if run_ids[run] == identity]
                keep(target, entry, str(candidates[0]) if len(candidates) == 1 else "")
                del pending[str(target.id).encode()]
            if not pending:
                break
    return RecordedWorkerDispatches(found, by_session)


class WorkerFilter(Enum):
    OPEN = "open"
    CLOSED = "closed"
    ALL = "all"


def _open_worker(state: worker_panes.WorkerPane) -> bool:
    return isinstance(state, (worker_panes.OpenWorker, worker_panes.StoppedWorker, worker_panes.ProcessUnknownWorker))


def _worker_is_parked(state: worker_panes.WorkerPane, dispatch: worker_panes.WorkerDispatchState) -> bool:
    return (isinstance(state, worker_panes.ClosedWorker) and isinstance(dispatch, worker_panes.DispatchHoldsWorker)
            and dispatch.parked and session_state.staff_state(state.row) == "active")


def _listed_worker(state: worker_panes.WorkerPane, selection: WorkerFilter, *, parked: bool) -> bool:
    return (selection is WorkerFilter.ALL or selection is WorkerFilter.OPEN and (_open_worker(state) or parked)
            or selection is WorkerFilter.CLOSED and isinstance(state, worker_panes.ClosedWorker))


def _waiting_on(row: staff.StaffRow, detached: session_state.RunningDetachedWork) -> str:
    return ", ".join(detached.labels.get(row.id, ())) or "-"


def list_all_workers(record: staff.StaffSnapshot, *, selection: WorkerFilter = WorkerFilter.ALL,
                     as_json: bool = True) -> list[dict[str, object]]:
    read = cache(worker_panes.run_state)
    dispatches = _recorded_worker_dispatches(record, read=read)
    workers = record.find(kind="worker")
    directories = worker_panes.worker_run_directories(record, scanned=dispatches.by_session, read=read)
    detached = _worker_detached_work(record, directories, (row.id for row in workers))
    states = {state.row.id: state for parent_id in dict.fromkeys(
        row.parent.id for row in workers if isinstance(row.parent, staff.ReportsTo))
        for state in worker_panes.observe(parent_id, record)} if selection is not WorkerFilter.ALL or not as_json else {}
    listing: list[dict[str, object]] = []
    with progress_history.dispatch_reads(record, ()):
        for row in workers:
            parent = record.parent_of(row)
            saved = dispatches.by_worker.get(row.id, WorkerDispatchRecord({}, 0))
            if parent is None:
                continue
            fields = saved.fields
            status = fields.get("status", "unknown")
            status = status if isinstance(status, str) else "unknown"
            activity = fields.get("kind") if status in ("active", "refused") else ""
            if isinstance(row.exited, staff.Exited):
                status = "exited"
            elif isinstance(row.paused, staff.Paused):
                status = "paused"
            work = "busy" if status == "active" else "idle" if status in ("completed", "error", "canceled", "interrupted", "paused", "exited") else "unknown"
            dispatch = _worker_dispatch_state(row, record, detached, read=read)
            observed = states.get(row.id)
            if observed is None and isinstance(dispatch, worker_panes.DispatchHoldsWorker) and dispatch.parked:
                found = worker_panes.observe_worker(parent.id, row, record)
                if not isinstance(found, worker_panes.WorkerNotObserved):
                    observed = found
            parked = observed is not None and _worker_is_parked(observed, dispatch)
            if selection is not WorkerFilter.ALL and (
                    observed is None or not _listed_worker(observed, selection, parked=parked)):
                continue
            pane = "-"
            if parked:
                pane, status, work = "closed", "parked", "parked"
            elif not as_json and isinstance(observed, worker_panes.OpenWorker):
                pane, displayed = _worker_pane_state(observed, dispatch)
                status = work = displayed
            elif status not in ("paused", "exited"):
                if isinstance(dispatch, worker_panes.DispatchHoldsWorker):
                    status, work = "active", "busy"
                elif status == "active":
                    status = dispatch.value if isinstance(dispatch, worker_panes.ReleasedWorkerPass) else "completed"
                    work = "idle"
            item = _worker_list_row(row, {"manager": parent.name, "state": status, "work": work,
                "activity": fields.get("activity") or status,
                **{key: fields.get(key, "unknown" if key == "completion_state" else "")
                   for key in ("dispatch_id", "run_path", "completion_state", "reply_path")}})
            if not as_json:
                item["list_activity"] = activity or item["role"]
                if pane != "-":
                    item["pane"] = pane
            if _waiting_on_done(row, detached, record):
                item["waiting_on"] = "transcript verification"
            elif parked or not as_json:
                item["waiting_on"] = _waiting_on(row, detached)
            listing.append(item)
    if not as_json:
        listing.sort(key=lambda fields: not (StaffId(str(fields["id"])) in states
                     and _open_worker(states[StaffId(str(fields["id"]))])))
    return listing


def _worker_pane_state(state: worker_panes.WorkerPane,
                       dispatch: worker_panes.WorkerDispatchState) -> tuple[str, str]:
    row = state.row
    if _worker_is_parked(state, dispatch):
        return "closed", "parked"
    activity = worker_panes.worker_state(state)
    if isinstance(state, worker_panes.OpenWorker):
        classified = session_state.classify(row, session_state.LiveActivity(
            "starting" if state.status == "starting" else activity))
        if classified in ("paused", "exited", "busy"):
            return "open", classified
        if isinstance(dispatch, worker_panes.DispatchHoldsWorker):
            return "open", "busy"
        return "open", activity
    if isinstance(state, worker_panes.StoppedWorker):
        pane, displayed = "open", "exited"
    elif isinstance(state, worker_panes.ClosedWorker):
        pane, displayed = "closed", "-"
    else:
        pane, displayed = "unknown", "-"
    if isinstance(dispatch, worker_panes.DispatchHoldsWorker) and session_state.staff_state(row) == "active":
        return pane, "busy"
    return pane, displayed


def list_workers(parent: StaffId, record: staff.StaffSnapshot, *, as_json: bool = True,
                 selection: WorkerFilter = WorkerFilter.ALL) -> list[dict[str, object]]:
    listing: list[dict[str, object]] = []
    rows = tuple(row for row in record.rows if row.kind != "worker" or row.reports_to(parent))
    directories = worker_panes.worker_run_directories(replace(record, rows=rows))
    detached = _worker_detached_work(record, directories, directories.by_worker)
    observed = _read_workers(next(row for row in record.rows if row.id == parent), record,
                             scanned=detached.launcher_runs.by_session)
    dispatches = (_recorded_worker_dispatches(record, runs=observed.runs)
                  if as_json else RecordedWorkerDispatches({}, {}))
    runs = dict.fromkeys((*observed.runs, *(path for paths in directories.by_worker.values() for path in paths)))
    with progress_history.dispatch_reads(record, runs):
        states = observed.states if as_json else sorted(observed.states, key=lambda state: not _open_worker(state))
        for state in states:
            dispatch_state = _worker_dispatch_state(state.row, record, detached)
            pane, displayed_state = _worker_pane_state(state, dispatch_state)
            parked = displayed_state == "parked"
            if not _listed_worker(state, selection, parked=parked):
                continue
            work = (displayed_state if isinstance(state, worker_panes.OpenWorker) or displayed_state in ("busy", "parked")
                    else worker_panes.worker_state(state))
            facts: dict[str, object] = {"state": type(state).__name__, "work": work}
            if as_json:
                saved = dispatches.by_worker.get(state.row.id, WorkerDispatchRecord({}, 0)).fields
                facts.update({key: saved.get(key, "unknown" if key == "completion_state" else "")
                              for key in ("dispatch_id", "run_path", "completion_state", "reply_path")})
            fields = _worker_list_row(state.row, facts)
            if isinstance(state, worker_panes.OpenWorker):
                fields["activity"] = state.status.value
            if isinstance(state, (worker_panes.ProcessUnknownWorker, worker_panes.UncertainWorker)):
                fields["reason"] = state.reason
            if not as_json:
                fields["pane"], fields["state"] = pane, displayed_state
                dispatch = progress_history.last_dispatch(state.row.id)
                fields["list_activity"] = (dispatch.role if isinstance(dispatch, progress_history.RecordedDispatch)
                                           and dispatch.status in ("active", "refused") else fields["role"])
            if _waiting_on_done(state.row, detached, record):
                fields["waiting_on"] = "transcript verification"
            elif parked or not as_json:
                fields["waiting_on"] = _waiting_on(state.row, detached)
            listing.append(fields)
    return listing


def _print_workers(listing: list[dict[str, object]], as_json: bool, record: staff.StaffSnapshot) -> ExitCode:
    if as_json:
        print(json.dumps(listing))
    else:
        locations: list[str] = []
        for fields in listing:
            row = record.by_id(StaffId(str(fields.get("id", ""))))
            parent = record.parent_of(row) if row is not None else None
            locations.append(staff.worktree_machine(parent) if parent is not None else "")
        machines = display_cells.machine_column(locations)
        rows = [["#", "Name", "Account", "Activity", "Pane", "State", "Waiting on"]]
        for number, fields in enumerate(listing, 1):
            activity = fields.get("list_activity", fields.get("role"))
            activity = activity if isinstance(activity, str) and activity else "activity unknown"
            account = fields.get("account")
            account = account if isinstance(account, str) else "account unknown"
            pane = fields.get("pane", "-")
            state = fields["state"]
            if pane == "-":
                state = state if state in ("paused", "exited") else "busy" if state == "active" else "idle"
            elif pane != "open":
                account = "-"
            machine = f" ({machines[number - 1]})" if machines else ""
            reason = f" ({fields['reason']})" if fields.get("reason") else ""
            rows.append([str(number), str(fields["name"]), account + machine, activity, str(pane), str(state) + reason,
                         " ".join(str(fields.get("waiting_on", "-")).splitlines())])
        widths = [max(len(row[column]) for row in rows) for column in range(len(rows[0]))]
        for row in rows:
            print("  ".join(cell.rjust(widths[column]) if column == 0 else cell.ljust(widths[column])
                            for column, cell in enumerate(row)).rstrip())
    return ExitCode.OK


def _launcher_arguments(text: str) -> list[str]:
    value = cast(object, json.loads(text))
    if not isinstance(value, list) or not value or any(not isinstance(item, str) for item in cast(list[object], value)):
        raise ValueError("--launcher-command requires a nonempty JSON list of arguments")
    return cast(list[str], value)


@final
class Arguments(argparse.Namespace):
    command: str = ""
    parent: str = ""
    role: str = ""
    name: str = ""
    prompt: str = ""
    text: str = ""
    session_dir: str = ""
    slot: str = ""
    phase: str = ""
    kind: str = ""
    lens: str = ""
    reply: str = ""
    dispatch_id: str = ""
    completion_id: str = ""
    lock_fd: int = -1
    started_fd: int = -1
    worker_id: str = ""
    fix_cause: str = ""
    fix_pass: int = 0
    activity: str = ""
    early_ready: str = ""
    launcher_command: str = ""
    model: str = ""
    effort: str = ""
    completion_actions: str = ""
    json: bool = False
    all_parents: bool = False
    new_pass_instance_id: uuid.UUID | None = None
    selection: WorkerFilter = WorkerFilter.OPEN


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("list", "restore", "check"):
        child = commands.add_parser(command)
        if command == "list":
            _ = child.add_argument("--json", action="store_true")
            _ = child.add_argument("selection", nargs="?", type=WorkerFilter,
                                   choices=list(WorkerFilter), default=WorkerFilter.OPEN, metavar="open|closed|all")
            _ = child.add_argument("--all-parents", action="store_true", help="read saved dispatches for every parent")
    answering = commands.add_parser("answer")
    _ = answering.add_argument("name")
    _ = answering.add_argument("--text", required=True)
    ending = commands.add_parser("end")
    _ = ending.add_argument("name")
    _ = ending.add_argument("--session-dir", default="")
    _ = ending.add_argument("--parent", default="")
    adding = commands.add_parser("add")
    _ = adding.add_argument("role", choices=staff.WORKER_ROLES)
    _ = adding.add_argument("--name", default="")
    _ = adding.add_argument("--json", action="store_true")
    _ = adding.add_argument("--session-dir", default="")
    assignment = commands.add_parser("assign")
    _ = assignment.add_argument("--parent", default="")
    _ = assignment.add_argument("role", choices=staff.WORKER_ROLES)
    _ = assignment.add_argument("prompt")
    for flag in ("session-dir", "slot", "phase", "kind", "lens", "name", "reply", "dispatch-id", "fix-cause", "activity", "early-ready", "model", "effort"):
        aliases = {"name": "--to", "reply": "--reply-file"}
        flags = ["--" + flag, *([aliases[flag]] if flag in aliases else [])]
        _ = assignment.add_argument(*flags, default="", required=flag in ("session-dir", "slot", "phase", "kind"))
    _ = assignment.add_argument("--fix-pass", "--pass", type=int, default=0)
    _ = assignment.add_argument("--launcher-command", required=True, help="JSON list of launcher arguments")
    _ = assignment.add_argument("--completion-actions", default="{}")
    _ = assignment.add_argument("--new-pass-instance-id", type=uuid.UUID, help=argparse.SUPPRESS)
    finishing = commands.add_parser("finish-dispatch", help=argparse.SUPPRESS)
    for flag in ("parent", "worker-id", "session-dir", "dispatch-id", "completion-id"):
        _ = finishing.add_argument("--" + flag, required=True)
    _ = finishing.add_argument("--lock-fd", type=int, default=-1)
    _ = finishing.add_argument("--started-fd", type=int, default=-1)
    args = parser.parse_args(arguments, namespace=Arguments())
    if args.command in ("add", "assign") and quota_state.no_worker_mode():
        print(f"worker.py: {quota_state.NO_WORKER_START}", file=sys.stderr)
        return ExitCode.REFUSED
    try:
        if args.command == "finish-dispatch":
            return finish_dispatch(StaffId(args.parent), StaffId(args.worker_id), Path(args.session_dir),
                                   args.dispatch_id, args.completion_id, lock=file_lock.ExclusiveLock(args.lock_fd)
                                   if args.lock_fd >= 0 else CompactionLockAtUse.ACQUIRE, started_fd=args.started_fd)
        if args.command == "list" and args.all_parents:
            record = staff.local_snapshot()
            return _print_workers(list_all_workers(record, selection=args.selection, as_json=args.json), args.json, record)
        record = staff.snapshot()
        owner = record.by_id(StaffId(args.parent)) if args.parent else record.by_current_session(staff.runtime_id())
        if owner is None or owner.kind not in ("unit", "showrunner"):
            print("staff: caller: not opened: caller is not a unit or showrunner", file=sys.stderr)
            return ExitCode.REFUSED
        if args.command == "list":
            return _print_workers(list_workers(owner.id, record, as_json=args.json, selection=args.selection), args.json, record)
        if args.command == "check":
            observed = _read_workers(owner, record)
            with progress_history.dispatch_reads(record, observed.runs):
                wrong = [state for state in observed.states
                         if not isinstance(state, (worker_panes.OpenWorker, worker_panes.StoppedWorker, worker_panes.ClosedWorker))
                         or (not isinstance(state, worker_panes.OpenWorker)
                             and isinstance(worker_panes.dispatch_role(state.row), worker_panes.WorkerRoleUnavailable))]
            for state in wrong:
                print(f"{state.row.name}: {getattr(state, 'reason', 'dispatch role is unavailable')}")
            return int(bool(wrong))
        if args.command == "restore":
            return restore(owner.id)
        if args.command == "answer":
            code = answer(owner.id, WorkerName.parse(args.name), args.text)
        elif args.command == "end":
            code = end(owner.id, WorkerName.parse(args.name))
        elif args.command == "add":
            result = add(owner.id, cast(WorkerRole, args.role), name=args.name)
            if args.json and isinstance(result, worker_panes.OpenWorker):
                print(json.dumps(result.row.to_json()))
                return ExitCode.OK
            code = startup_result(WorkerName.parse(args.role), result)
        else:
            policy: worker_panes.UseRolePolicy | worker_panes.LaunchPolicy = worker_panes.UseRolePolicy.CURRENT
            if args.model or args.effort:
                selected = worker_panes.launch_policy(cast(WorkerRole, args.role))
                policy = replace(selected, settings=selected.settings._replace(
                    model=args.model or selected.settings.model, effort=args.effort or selected.settings.effort))
            code = assign(owner.id, cast(WorkerRole, args.role), Path(args.prompt), session_dir=Path(args.session_dir),
                slot=args.slot, phase=args.phase, kind=args.kind, lens=args.lens, name=args.name,
                reply=Path(args.reply) if args.reply else REPLY_BESIDE_PROMPT, dispatch_id=args.dispatch_id,
                launcher_command=_launcher_arguments(args.launcher_command), fix_cause=args.fix_cause,
                fix_pass=args.fix_pass, activity=args.activity,
                early_ready=Path(args.early_ready) if args.early_ready else SINGLE_STAGE_REVIEW, policy=policy,
                completion_actions=pass_recorder.dispatch_delivery({"delivery": {"actions": cast(object, json.loads(args.completion_actions))}}).actions,
                new_pass_instance_id=args.new_pass_instance_id)
        if isinstance(code, DispatchSubmission):
            print("submitted")
        elif code == ExitCode.OK:
            print("done")
        return code
    except recorder_state.OldRecorderState as error:
        print(str(error), file=sys.stderr)
        return ExitCode.UNCERTAIN
    except (staff.StaffUnreadable, staff.StaffRefused, herdr_types.HerdrFailure, OSError, ValueError) as error:
        print(f"anomaly: {' '.join(str(error).splitlines())}", file=sys.stderr)
        return ExitCode.UNCERTAIN


if __name__ == "__main__":
    raise SystemExit(main())

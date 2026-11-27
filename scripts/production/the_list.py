"""Keep The List as numbered records under morty's fixed staff ID."""
from __future__ import annotations

try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import ast
import hashlib
import json
import os
import re
import secrets
import shlex
import signal
import subprocess
import sys
import threading
from bisect import bisect_right
from collections.abc import Callable, Generator, Iterable, Iterator, Sequence
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta, timezone, tzinfo
from enum import Enum, StrEnum
from functools import cache
from heapq import merge
from importlib import import_module
from itertools import chain, dropwhile, islice, takewhile
from pathlib import Path
from tempfile import TemporaryFile
from time import monotonic
from typing import IO, TYPE_CHECKING, Literal, TypedDict, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from scripts.agents import remote
from scripts.console_text import check as console_text
from scripts.lib import conf, operation_deadline, process_group
from scripts.lib.atomic_file import AtomicDurabilityUnconfirmed, sync_folder, write_text_atomic
from scripts.lib.file_lock import LockResult, exclusive
from scripts.lib.user_zone import user_zone, zone_label
from scripts.message import send_result, session_send
from scripts.notify import push_texts
from scripts.production import list_exchange
from scripts.production import delivery_state, goal, investigation, producer_map, record_store, repeat_rule, session_pane, staff
from scripts.review_links import change_page, list_draft, page_frame, page_question, question_path, question_record
from scripts.review_links.review_items import LegacyValidationLog, ValidationLogStart


if TYPE_CHECKING:
    from scripts.review_links import review_items, review_submissions


class ListError(ValueError):
    """The item cannot accept the requested operation."""


class ScheduleSelectionChanged(ListError):
    """The saved schedule or selected run changed before the write."""


class ConflictingExchangeReplies(ListError):
    """The source has no exchange without a reply conflict."""


class SavedReadFailed(OSError):
    """The save is on disk, but the item could not be read again."""


class RetiredListProducer(ListError):
    """A retired producer cannot take an open List item."""


class ItemState(StrEnum):
    NEW = "New"
    WORKING = "Working"
    ANSWERED = "Answered"
    SCHEDULED = "Scheduled"
    BLOCKED = "Blocked"
    WAITING_FOR_NATE = "Waiting for nate"
    REPAIRED = "Repaired"
    DELIVERED = "Delivered"
    DONE = "Done"
    PAUSED = "Paused"
    CLOSED = "Closed"


class ReportKind(StrEnum):
    BUG = "Bug"
    ENHANCEMENT = "Enhancement"
    INVESTIGATION = "Investigation"
    CHANGE = "Change"
    VALIDATION = "Validation"
    QUESTION = "Question"
    PROPOSAL = "Proposal"
    EFFICIENCY = "Efficiency"


class NoReportScope(StrEnum):
    NATIVE_RECORD = ""


@dataclass(frozen=True)
class ListReport:
    """Keep the original report and its native evidence key."""

    kind: ReportKind
    evidence_key: str
    words: str
    submission_scope: str | NoReportScope = NoReportScope.NATIVE_RECORD
    validation_log: ValidationLogStart | LegacyValidationLog = LegacyValidationLog.MOVE_MARKER


class NoListReport(Enum):
    ABSENT = "ordinary List item"


class ReportRetry(Enum):
    KEEP = "keep the filed report"
    UPDATE = "update the report before the action saves"


ReportVerdict = page_frame.VerdictAction


@dataclass(frozen=True)
class RepairRevision:
    """Identify the repair and instructions that nate saw."""

    digest: str


class NoRepairRevision(Enum):
    ABSENT = "the item has no repair to check"


class RequestedBy(StrEnum):
    NATE = "nate"
    DWIGHT = "dwight"


@dataclass(frozen=True)
class ReviewByShownId:
    """Select a review through its shown ID, which can be ambiguous."""

    value: str


@dataclass(frozen=True)
class ReviewByKey:
    """Select a review through an exact key."""

    value: str


class NoTimestamp(StrEnum):
    UNSET = ""


Timestamp = datetime | NoTimestamp


ParkUntilDelivered = question_path.UntilDelivered
Parking = question_path.LaterValue | NoTimestamp


class EtaStatus(StrEnum):
    NOT_ASKED = "Not asked"
    PENDING = "Pending"
    NO_TIME_YET = "No time yet"
    WAITING_FOR_NATE = "Waiting for nate"
    PAUSED = "Paused"
    DELIVERED = "Delivered"


@dataclass(frozen=True)
class EtaRange:
    earliest: datetime
    latest: datetime


@dataclass(frozen=True)
class EtaDescription:
    text: str


@dataclass(frozen=True)
class CheckCondition:
    text: str


class UnassignedOwner(StrEnum):
    NATE = "nate"
    NOT_RECORDED = "Not recorded"


ItemOwner = staff.StaffId | UnassignedOwner
NextCheck = Timestamp | CheckCondition
Eta = datetime | date | EtaRange | EtaStatus | EtaDescription


@dataclass(frozen=True)
class ItemDependency:
    number: int
    condition: str


@dataclass(frozen=True)
class ItemChange:
    time: datetime
    what: LogContent
    source: str


@dataclass(frozen=True)
class _ImageAnswerChange(ItemChange):
    what: str
    key: str
    producer: staff.StaffId


@dataclass(frozen=True)
class UnreadUpdate:
    """Identify the unread update that a feedback request can mark read."""

    fingerprint: str


class NoUnreadUpdate(StrEnum):
    ABSENT = ""


class ReadCurrentUpdate(StrEnum):
    CURRENT = "current update"


@dataclass(frozen=True)
class PageRequest:
    """Identify the action and its input for a retry."""

    request: str
    action: Literal["tell", "remove", "park", "unpark", "read", "question", ReportVerdict.WORKS, ReportVerdict.NOT_FIXED]
    words: str
    form_words: str = ""
    parked_until: Parking = NoTimestamp.UNSET


class NoPageRequest(StrEnum):
    ABSENT = ""


@dataclass(frozen=True)
class QuestionRequestReceipt:
    request: PageRequest
    question_id: str


class UnparkNoChangeReason(StrEnum):
    EXPIRED = "expired"
    NO_ACTIVE_PARKING = "no_active_parking"


@dataclass(frozen=True)
class UnparkNoChangeReceipt:
    request: PageRequest
    reason: UnparkNoChangeReason


class LegacyFeedbackRecipient(StrEnum):
    KEEPER = ""


type FeedbackRecipient = staff.StaffId | LegacyFeedbackRecipient


class NoAdditionalRecipient(Enum):
    ABSENT = "absent"


class PageNoticeOrigin(StrEnum):
    LIST_PAGE = "List page"
    STUDIO = "Studio"


@dataclass(frozen=True)
class PageFeedback:
    request: str
    words: str
    state: Literal["pending", "sent", "alerted"] = "pending"
    alerted_to: str = ""
    read_update: UnreadUpdate | NoUnreadUpdate = NoUnreadUpdate.ABSENT
    action: Literal["tell", "remove", "park", "unpark", "read", ReportVerdict.NOT_FIXED] = "tell"
    form_words: str = ""
    parked_until: Parking = NoTimestamp.UNSET
    recipient: FeedbackRecipient = LegacyFeedbackRecipient.KEEPER

    additional_recipient: staff.StaffId | NoAdditionalRecipient = NoAdditionalRecipient.ABSENT
    origin: PageNoticeOrigin = PageNoticeOrigin.LIST_PAGE

    def key(self) -> PageRequest:
        return PageRequest(self.request, self.action, self.words, self.form_words, self.parked_until)

    def recipient_id(self, keeper: staff.StaffId) -> staff.StaffId:
        return keeper if self.recipient is LegacyFeedbackRecipient.KEEPER else self.recipient

    def recipient_ids(self, keeper: staff.StaffId) -> tuple[staff.StaffId, ...]:
        recipient = self.recipient_id(keeper)
        extra = self.additional_recipient
        if extra is NoAdditionalRecipient.ABSENT or extra == recipient:
            return (recipient,)
        return recipient, extra


@dataclass(frozen=True)
class PageItemNotice:
    """Save an item action and its notice in the same write."""

    request: str
    words: str
    action: Literal["remove", "park", "unpark", ReportVerdict.NOT_FIXED]
    form_words: str = ""
    parked_until: Parking = NoTimestamp.UNSET
    recipient: FeedbackRecipient = LegacyFeedbackRecipient.KEEPER
    origin: PageNoticeOrigin = PageNoticeOrigin.LIST_PAGE


class AskKind(StrEnum):
    GO = "go"
    APPROVAL = "approval"


ASK_ID = re.compile(r"A[1-9][0-9]*")


@dataclass(frozen=True)
class Ask:
    """Name one action that waits for nate's Go or Approved on the List page."""

    ask_id: str
    kind: AskKind
    what: str
    why: str
    target: staff.StaffId
    config: bool
    opened: datetime
    closed: Timestamp = NoTimestamp.UNSET
    go_id: str = ""
    withdrawn: bool = False

    @property
    def is_open(self) -> bool:
        return not isinstance(self.closed, datetime)


PAGE_SOURCE = "nate via List"
_IMAGE_ANSWER_SOURCE = "nate via Images"
_LEGACY_RECEIPT = "List request receipt: "
_LEGACY_FEEDBACK = "Feedback to morty: "
feedback_claims: set[tuple[Path, str]] = set()
feedback_claims_lock = threading.Lock()


class NoSchedule(StrEnum):
    UNSET = ""


@dataclass(frozen=True)
class ScheduledRun:
    start: datetime
    words: str


@dataclass(frozen=True)
class SkippedRun:
    words: str


@dataclass(frozen=True)
class RunException:
    number: int
    run: ScheduledRun | SkippedRun


class UseRuleEnd(StrEnum):
    RULE = "use the repeat rule"


@dataclass(frozen=True)
class RetainedFollowingRuns:
    numbers: frozenset[int]


@dataclass(frozen=True)
class ScheduledSeries:
    """Keep the run numbers and allowance across edits."""

    first: ScheduledRun
    rule: repeat_rule.RepeatRule
    run_allowance: int
    run_number_offset: int = 0
    exceptions: tuple[RunException, ...] = ()
    runs_used: int = 0
    finished_runs: frozenset[int] = frozenset()
    following_runs: RetainedFollowingRuns | UseRuleEnd = UseRuleEnd.RULE

    @property
    def skipped_runs(self) -> frozenset[int]:
        return frozenset(entry.number for entry in self.exceptions if isinstance(entry.run, SkippedRun))

    def finish_runs(self, numbers: Iterable[int]) -> ScheduledSeries:
        """Record finished runs after delivery or recovery."""
        requested = tuple(numbers)
        if any(type(number) is not int for number in requested):
            raise ListError("a run number must be an integer")
        new = frozenset(requested) - self.finished_runs - self.skipped_runs
        if not new:
            return self
        unissued = set(new)
        for run in self.runs():
            unissued.discard(run.number)
            if not unissued:
                break
        if unissued:
            raise ListError(f"the series did not issue run {min(unissued)}")
        return replace(self, finished_runs=self.finished_runs | new, runs_used=self.runs_used + len(new))

    def pending_count(self, end: repeat_rule.RunCount) -> int:
        settled = self.finished_runs | self.skipped_runs
        if isinstance(self.following_runs, RetainedFollowingRuns):
            return len(self.following_runs.numbers - settled)
        used = sum(self.run_number_offset < number <= self.run_number_offset + end.count for number in settled)
        return end.count - used

    def validate_run_times(self) -> None:
        instants: set[datetime] = set()
        for slot in self._slots():
            instant = slot.run.start.astimezone(UTC)
            if instant in instants:
                raise ListError("another run already uses this time")
            instants.add(instant)

    def _slots(self) -> Generator[SeriesRun]:
        exceptions = {entry.number: entry.run for entry in self.exceptions}
        settled = self.finished_runs | self.skipped_runs
        remaining = max(0, self.run_allowance - self.runs_used)
        retained = self.following_runs
        rule = self.rule
        if isinstance(retained, RetainedFollowingRuns):
            slot_limit = max(retained.numbers) - self.run_number_offset
            rule = replace(rule, end=repeat_rule.RunCount(slot_limit))
        else:
            slot_limit = remaining + sum(number > self.run_number_offset for number in settled)
        for number, original in enumerate(rule.runs(self.first.start, slot_limit), self.run_number_offset + 1):
            if isinstance(retained, RetainedFollowingRuns):
                if number not in retained.numbers and number not in self.finished_runs:
                    continue
            elif number not in settled:
                if remaining == 0:
                    break
                remaining -= 1
            run = exceptions.get(number, ScheduledRun(original, self.first.words))
            if isinstance(run, ScheduledRun):
                yield SeriesRun(number, original, run)

    def runs(self) -> Generator[SeriesRun]:
        """Yield pending runs in effective time order."""
        pending: Iterator[SeriesRun] = (run for run in self._slots() if run.number not in self.finished_runs)
        last_override = max((entry.number for entry in self.exceptions if isinstance(entry.run, ScheduledRun)),
                            default=self.run_number_offset)
        if last_override > self.run_number_offset:
            prefix: list[SeriesRun] = []
            for run in pending:
                if run.number > last_override:
                    pending = chain((run,), pending)
                    break
                prefix.append(run)

            def effective_start(run: SeriesRun) -> datetime:
                return run.run.start.astimezone(UTC)

            prefix.sort(key=effective_start)
            pending = merge(prefix, pending, key=effective_start)
        remaining = max(0, self.run_allowance - self.runs_used)
        yield from islice(pending, min(repeat_rule.MAX_RUN_DATES, remaining))

    def select_following_runs(self, selected: SeriesRun, rule: repeat_rule.RepeatRule,
                              run_allowance: int) -> RetainedFollowingRuns:
        """Select runs from the selected effective time through the new end.

        Keep eligible pending runs, including moved runs, in effective time order.
        For COUNT, keep the earliest pending runs up to the requested count.
        Fill any remaining places with new rule runs in time order.
        Deliver all selected runs in effective time order.
        """
        end = rule.end
        start = selected.run.start.astimezone(UTC)
        remaining = run_allowance - self.runs_used
        if remaining <= 0:
            raise ListError(f"the series has no runs left under the limit of {run_allowance}")
        count = end.count if isinstance(end, repeat_rule.RunCount) else remaining
        if count > remaining:
            raise ListError(f"COUNT exceeds the series limit of {run_allowance} runs")
        if isinstance(end, repeat_rule.RunUntil) and end.utc < start:
            raise ListError("UNTIL must be on or after the selected run")

        def in_scope(runs: Iterable[SeriesRun]) -> Iterator[int]:
            pending = dropwhile(lambda run: run.run.start.astimezone(UTC) < start, runs)
            if isinstance(end, repeat_rule.RunUntil):
                pending = takewhile(lambda run, cutoff=end.utc: run.run.start.astimezone(UTC) <= cutoff, pending)
            return (run.number for run in pending)

        open_rule = replace(rule, end=repeat_rule.RepeatUntilLimit.REACHED)
        search_allowance = self.runs_used + repeat_rule.MAX_RUN_DATES
        open_series = replace(self, rule=open_rule, run_allowance=search_allowance, following_runs=UseRuleEnd.RULE)
        numbers: set[int] = set()
        for number in chain(in_scope(self.runs()), in_scope(open_series.runs())):
            numbers.add(number)
            if len(numbers) == count:
                break
        if isinstance(end, repeat_rule.RunCount) and len(numbers) < count:
            raise ListError("COUNT exceeds the available following runs")
        return RetainedFollowingRuns(frozenset(numbers))

    def describe(self) -> str:
        rule = self.rule
        if isinstance(rule.end, repeat_rule.RunCount):
            rule = replace(rule, end=repeat_rule.RunCount(len(tuple(self.runs()))))
        return rule.describe(self.first.start)

    def next_run(self) -> SeriesRun | repeat_rule.NoNextRun:
        return next(self.runs(), repeat_rule.NoNextRun.REMAINING)


@dataclass(frozen=True)
class SeriesRun:
    number: int
    original: datetime
    run: ScheduledRun


Schedule = NoSchedule | ScheduledRun | ScheduledSeries


class AwaitingInstruction(StrEnum):
    OWNER = "waiting for instruction"


@dataclass(frozen=True)
class AwaitingConfirmation:
    deadline: datetime


class RunResult(StrEnum):
    CONFIRMED = "confirmed"
    NOT_CONFIRMED = "not confirmed"
    MANUALLY_SETTLED = "manually settled"


RunConfirmation = AwaitingInstruction | AwaitingConfirmation | RunResult


class RunNotice(StrEnum):
    OWNER_START = "owner start"
    PRODUCER_START = "producer start"
    PRODUCER_TIMEOUT = "producer timeout"
    USER_TIMEOUT = "user timeout"


_START_NOTICES = (RunNotice.OWNER_START, RunNotice.PRODUCER_START)
_TIMEOUT_NOTICES = (RunNotice.USER_TIMEOUT, RunNotice.PRODUCER_TIMEOUT)


class NoticeState(StrEnum):
    READY = "ready"
    SENDING = "sending"
    ACCEPTED = "accepted"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class ScheduledNotice:
    kind: RunNotice
    state: NoticeState = NoticeState.READY

    def needs_check(self) -> bool:
        return self.state in (NoticeState.READY, NoticeState.SENDING)


@dataclass(frozen=True)
class RunDelivery:
    key: str
    run: ScheduledRun
    title: str
    owner: staff.StaffId
    producer: staff.StaffId
    log_index: int
    confirmation: RunConfirmation
    notices: tuple[ScheduledNotice, ...]

    def log_text(self) -> str:
        due = _schedule_time_text(self.run.start)
        match self.confirmation:
            case RunResult.CONFIRMED:
                status = "Confirmed."
            case RunResult.NOT_CONFIRMED:
                status = "No confirmation."
            case RunResult.MANUALLY_SETTLED:
                status = "Settled by a manual state change."
            case AwaitingInstruction.OWNER:
                status = "Sending the instruction."
            case AwaitingConfirmation():
                owner = next(notice for notice in self.notices if notice.kind is RunNotice.OWNER_START)
                status = ("Instruction sent. Waiting for confirmation." if owner.state is NoticeState.ACCEPTED
                          else "Instruction uncertain. Waiting for confirmation.")
        return f"Scheduled run due {due}. {status}"


class NoDueRun(StrEnum):
    YET = "no run due"


@dataclass(frozen=True)
class DueRuns:
    starts: tuple[ScheduledRun, ...]
    remaining: Schedule
    missed: tuple[ScheduledRun, ...] = ()


class DeliveryUncertainty(StrEnum):
    NONE = "no uncertain notice"


@dataclass(frozen=True)
class UncertainNotice:
    kind: RunNotice


class ScheduledCheckState(StrEnum):
    READY = "check now"
    IDLE = "no check due"


@dataclass(frozen=True)
class ScheduledCheckAt:
    time: datetime


class ScheduleScope(StrEnum):
    UNSPECIFIED = ""
    THIS = "this"
    FOLLOWING = "following"
    ALL = "all"


class RemoveRepeat(StrEnum):
    KEEP_ONE_RUN = "keep one scheduled run"


class UseCurrentSchedule(StrEnum):
    AT_WRITE = "use the schedule at the time of the write"


@dataclass(frozen=True)
class ScheduleExpectation:
    revision: str
    run_number: int | repeat_rule.NoNextRun


class ScheduleChanges(TypedDict, total=False):
    start: datetime
    words: str
    repeat: repeat_rule.RepeatRule | RemoveRepeat


class RepeatInSelectedRunZone(StrEnum):
    KEEP = "use the selected run's time zone for a new repeat rule"


class QuestionItemKind(StrEnum):
    TO_NATE = "question to nate"
    FROM_NATE = "question from nate"


class NoQuestionItem(Enum):
    ORDINARY = "ordinary List item"


class ScheduleOperation(StrEnum):
    EDIT = "schedule"
    REMOVE = "unschedule"
    START_NOW = "start-now"


@dataclass(frozen=True)
class ListItem:
    number: int
    title: str
    owner: ItemOwner
    producer: staff.StaffId
    unit: str
    state: ItemState
    next_check: NextCheck
    eta: Eta
    unread_since: Timestamp
    parked_until: Parking
    blocked_by: tuple[ItemDependency, ...]
    related_to: tuple[int, ...]
    requested_by: RequestedBy
    opened: Timestamp
    updated: Timestamp
    closed: Timestamp
    closing_words: str
    next_step: str
    latest_status: str
    evidence: str
    done_when: str
    log: tuple[ItemChange, ...]
    reviews: tuple[str, ...] = ()
    questions: tuple[question_record.Question, ...] = ()
    schedule: Schedule = NoSchedule.UNSET
    run_deliveries: tuple[RunDelivery, ...] = ()
    new_item_draft_id: list_draft.ListDraftOrigin = list_draft.NoNewItemDraft.ABSENT
    asks: tuple[Ask, ...] = ()
    report: ListReport | NoListReport = NoListReport.ABSENT
    filed_exchanges: tuple[list_exchange.FiledExchange, ...] = ()
    question_kind: QuestionItemKind | NoQuestionItem = NoQuestionItem.ORDINARY

    def __post_init__(self) -> None:
        if self.state != ItemState.CLOSED or not isinstance(self.unread_since, datetime):
            return
        # A Closed item can have an unread answer after its last close.
        for entry in reversed(self.log):
            if isinstance(entry.what, (QuestionAnswered, ReturnedAnswer)):
                if entry.time == self.unread_since:
                    return
                break
            if isinstance(entry.what, MarkedRead) or isinstance(entry.what, FieldChange) and entry.what.action == "Closed":
                break
        object.__setattr__(self, "unread_since", NoTimestamp.UNSET)

    @property
    def id(self) -> str:
        return f"L{self.number}"

    @property
    def state_entered(self) -> Timestamp:
        state = ItemState.NEW
        entered = self.opened
        for entry in self.log:
            event = entry.what
            next_state = state
            if isinstance(event, FieldChange):
                next_state = ItemState.CLOSED if event.action == "Closed" else event.fields.get("state", state)
            elif isinstance(event, Reopened) or event == "Reopened":
                next_state = ItemState.NEW
            if next_state != state:
                state, entered = next_state, entry.time
        return entered

    @property
    def repair_revision(self) -> RepairRevision | NoRepairRevision:
        if not isinstance(self.report, ListReport) or self.state != ItemState.REPAIRED:
            return NoRepairRevision.ABSENT
        text = json.dumps((str(self.state_entered), self.next_step, self.owner, self.producer), ensure_ascii=False)
        return RepairRevision(hashlib.sha256(text.encode()).hexdigest())

    @property
    def latest_status_time(self) -> Timestamp:
        for entry in reversed(self.log):
            if isinstance(entry.what, FieldChange) and "latest_status" in entry.what.fields:
                return entry.time
        return NoTimestamp.UNSET


def native_question_kind(exchange: list_exchange.Exchange) -> QuestionItemKind | NoQuestionItem:
    if exchange.source is list_exchange.Source.STAFF_QUESTION and exchange.source_key.startswith("question/"):
        return QuestionItemKind.TO_NATE
    if exchange.source is list_exchange.Source.SCOPED_REPLY and exchange.source_key.startswith("submission/question/"):
        return QuestionItemKind.FROM_NATE
    return NoQuestionItem.ORDINARY


def question_item_exchanges(item: ListItem) -> tuple[list_exchange.FiledExchange, ...]:
    """Find the native question that created this item."""
    if not item.log or isinstance(item.report, ListReport):
        return ()
    origin = item.log[0].source
    if origin == PAGE_SOURCE:
        if not item.filed_exchanges or native_question_kind(item.filed_exchanges[0]) is NoQuestionItem.ORDINARY:
            return ()
        key = item.filed_exchanges[0].source_key
        return tuple(saved for saved in item.filed_exchanges if saved.source_key == key)
    for exchange in item.filed_exchanges:
        if (origin in ("nate via New item", "nate via /new_item")
                and native_question_kind(exchange) is QuestionItemKind.FROM_NATE
                and item.reviews and item.reviews[0].startswith("question/")):
            return tuple(saved for saved in item.filed_exchanges if saved.source_key == exchange.source_key)
    return ()


def question_item(item: ListItem) -> QuestionItemKind | NoQuestionItem:
    return item.question_kind


class AllItems(StrEnum):
    INCLUDED = "all items"


@dataclass(frozen=True)
class SavedEdit:
    before: ListItem
    item: ListItem


@dataclass(frozen=True)
class UnchangedEdit:
    item: ListItem


ListEdit = SavedEdit | UnchangedEdit


class ProducerRecordOmission(StrEnum):
    OUTSIDE_SCOPE = "outside the producer's scope"
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class ProducerListRead:
    items: list[ListItem]
    incomplete: bool


class ItemChanges(TypedDict, total=False):
    title: str
    owner: ItemOwner
    producer: staff.StaffId
    unit: str
    state: ItemState
    next_check: NextCheck
    eta: Eta
    blocked_by: tuple[ItemDependency, ...]
    related_to: tuple[int, ...]
    requested_by: RequestedBy
    next_step: str
    latest_status: str
    evidence: str
    done_when: str
    schedule: Schedule


class LogFields(ItemChanges, total=False):
    parked_until: Parking
    closing_words: str


@dataclass(frozen=True)
class FieldChange:
    action: Literal["Added", "Updated", "Parked", "Closed"]
    fields: LogFields


@dataclass(frozen=True)
class LegacyFieldChange(FieldChange):
    """Keep the original note for the unread update identity."""

    note: str


@dataclass(frozen=True)
class PageParkingChange(FieldChange):
    """Link the parking change to the List page request."""

    action: Literal["Parked"]
    request: str


@dataclass(frozen=True)
class QuestionAnswered:
    question_id: str


@dataclass(frozen=True)
class ReturnedAnswerId:
    """Identify the filed exchange that supplied the answer."""

    source_key: str
    exchange_id: str


@dataclass(frozen=True)
class ReturnedAnswer:
    identity: ReturnedAnswerId
    words: str


type UnreadAnswerId = str | ReturnedAnswerId


@dataclass(frozen=True)
class QuestionClosed:
    question: question_record.Question


@dataclass(frozen=True)
class MarkedRead:
    pass


@dataclass(frozen=True)
class Reopened:
    pass


@dataclass(frozen=True)
class Unparked:
    pass


class ItemDelivered(StrEnum):
    RECORDED = "delivered"


@dataclass(frozen=True)
class LinkedChangeDelivered:
    key: str


@dataclass(frozen=True)
class UnparkedForDelivery(Unparked):
    reason: ItemDelivered | LinkedChangeDelivered


@dataclass(frozen=True)
class ReviewLinksChanged:
    action: Literal["Linked reviews", "Unlinked reviews"]
    keys: tuple[str, ...]


@dataclass(frozen=True)
class EvidenceCopyRemoved:
    copy: Literal["checkins.json", "the List row"]


@dataclass(frozen=True)
class AskOpened:
    """Keep the ask as the producer opened it."""

    ask: Ask


@dataclass(frozen=True)
class AskWithdrawn:
    ask: Ask
    reason: str


@dataclass(frozen=True)
class GoGiven:
    """Keep the closed ask with the go id of nate's click."""

    ask: Ask


@dataclass(frozen=True)
class GoNotDelivered:
    """Show nate that the Go message did not reach one recipient."""

    ask: Ask
    recipient: staff.StaffId


PageActivity = PageFeedback | PageRequest | QuestionRequestReceipt | UnparkNoChangeReceipt
ListEvent = (FieldChange | QuestionAnswered | ReturnedAnswer | QuestionClosed | MarkedRead | Reopened | Unparked
             | ReviewLinksChanged | EvidenceCopyRemoved | AskOpened | AskWithdrawn | GoGiven | GoNotDelivered | PageActivity)
LogContent = str | ListEvent


class NextNumber(StrEnum):
    AVAILABLE = "next available number"


def _store(keeper: staff.StaffId) -> record_store.NumberedRecords:
    return list_draft.list_records(keeper, ListError)


def folder(keeper: staff.StaffId) -> Path:
    return _store(keeper).folder()


def now() -> datetime:
    return datetime.now(UTC)


def time_value(text: str) -> Timestamp:
    """Read an offset time or a local date; an empty field has no timestamp."""
    if not text:
        return NoTimestamp.UNSET
    value = datetime.fromisoformat(text)
    if len(text) == 10:
        value = value.replace(tzinfo=user_zone())
    _validate_time(value)
    return value


def read_parking_time(text: str) -> question_path.LaterValue:
    def local_date(value: datetime) -> datetime:
        if len(text) != 10:
            _validate_time(value)
        return value.replace(tzinfo=user_zone())

    return question_path.read_later(text, local_date)


def parking_value(text: str) -> Parking:
    return read_parking_time(text) if text else NoTimestamp.UNSET


def later_value(parking: Parking) -> question_path.Later:
    return question_path.NO_LATER if parking is NoTimestamp.UNSET else parking


def parking_active(value: Parking, moment: datetime) -> bool:
    return question_path.later_active(later_value(value), moment)


def require_active_parking(value: Parking, moment: datetime) -> None:
    """Refuse a parking deadline that has passed."""
    if not parking_active(value, moment):
        raise ListError("Choose a future parking date.")


def _request_parking(value: Parking) -> Parking:
    return value.astimezone(UTC) if isinstance(value, datetime) else value


def _required_time(text: str) -> datetime:
    value = time_value(text)
    if not isinstance(value, datetime):
        raise ListError("a required time is empty")
    return value


def _scheduled_time(text: str) -> datetime:
    stamp, bracket, zone = text.partition("[")
    if not re.match(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}", stamp):
        raise ListError("a schedule start needs an ISO date, time, and time zone")
    value = datetime.fromisoformat(stamp)
    if bracket:
        try:
            if not zone.endswith("]"):
                raise ValueError("missing closing bracket")
            named = ZoneInfo(zone[:-1])
        except (ZoneInfoNotFoundError, ValueError):
            raise ListError("use an IANA time zone such as [America/Los_Angeles]") from None
        try:
            local = value.astimezone(named) if value.utcoffset() is not None else value.replace(tzinfo=named)
        except OverflowError:
            raise ListError("the start is outside the supported date range in its time zone") from None
        if local.replace(tzinfo=None) != value.replace(tzinfo=None):
            _ = _validate_schedule_time(value.replace(tzinfo=named))
            raise ListError("the start offset does not match its time zone")
        value = local
    _ = _validate_schedule_time(value)
    return value


def _validate_schedule_time(value: datetime) -> datetime:
    try:
        return repeat_rule.start_instant(value)
    except ValueError as error:
        raise ListError(str(error)) from None


def _schedule_time_text(value: datetime) -> str:
    zone = value.tzinfo
    return value.isoformat() + (f"[{zone.key}]" if isinstance(zone, ZoneInfo) else "")


def _schedule_fields(schedule: Schedule) -> dict[str, object] | None:
    if isinstance(schedule, NoSchedule):
        return None
    first = schedule.first if isinstance(schedule, ScheduledSeries) else schedule
    fields: dict[str, object] = {"start": _schedule_time_text(first.start), "do": first.words}
    if isinstance(schedule, ScheduledSeries):
        fields.update(repeat=schedule.rule.rrule, run_allowance=schedule.run_allowance,
                      runs_before=schedule.run_number_offset, runs_used=schedule.runs_used,
                      finished_runs=sorted(schedule.finished_runs))
        if isinstance(schedule.following_runs, RetainedFollowingRuns):
            fields["following_runs"] = sorted(schedule.following_runs.numbers)
        fields["exceptions"] = [
            {"number": entry.number, "removed": True, "words": entry.run.words} if isinstance(entry.run, SkippedRun)
            else {"number": entry.number, "start": _schedule_time_text(entry.run.start), "do": entry.run.words}
            for entry in schedule.exceptions
        ]
    return fields


def _read_run_numbers(raw: object, label: str) -> frozenset[int]:
    if isinstance(raw, list):
        numbers = cast(list[object], raw)
        if all(type(number) is int for number in numbers):
            result = frozenset(cast(list[int], numbers))
            if len(result) == len(numbers):
                return result
    raise ListError(f"invalid {label} run numbers")


def _read_schedule(raw: object, legacy_allowance: Callable[[], int]) -> Schedule:
    if raw is None:
        return NoSchedule.UNSET
    fields = _json_fields(raw)
    first = ScheduledRun(_scheduled_time(_field_text(fields.get("start"), "start")), _field_text(fields.get("do"), "do"))
    schedule: Schedule = first
    if fields.keys() != {"start", "do"}:
        required = {"start", "do", "repeat", "runs_before", "exceptions"}
        with_progress = required | {"runs_used", "finished_runs"}
        schedule_keys = fields.keys() - {"run_allowance"}
        if schedule_keys not in (required, required | {"runs_used"}, with_progress, with_progress | {"following_runs"}):
            raise ListError("invalid schedule fields")
        run_allowance = fields["run_allowance"] if "run_allowance" in fields else legacy_allowance()
        if type(run_allowance) is not int or run_allowance < 1:
            raise ListError("invalid schedule run allowance")
        run_number_offset = fields["runs_before"]
        runs_used = fields.get("runs_used", run_number_offset)
        rows = fields["exceptions"]
        if (type(run_number_offset) is not int or type(runs_used) is not int or runs_used < 0
                or run_number_offset < 0 or not isinstance(rows, list)):
            raise ListError("invalid schedule run history")
        exceptions: list[RunException] = []
        for raw_row in cast(list[object], rows):
            entry = _json_fields(raw_row)
            number = entry.get("number")
            if type(number) is not int:
                raise ListError("a schedule exception needs a run number")
            if entry.keys() in ({"number", "removed"}, {"number", "removed", "words"}) and entry["removed"] is True:
                run: ScheduledRun | SkippedRun = SkippedRun(_field_text(entry.get("words", ""), "skip words"))
            elif entry.keys() == {"number", "start", "do"}:
                run = ScheduledRun(_scheduled_time(_field_text(entry["start"], "start")), _field_text(entry["do"], "do"))
            else:
                raise ListError("invalid schedule exception")
            exceptions.append(RunException(number, run))
        skipped = frozenset(entry.number for entry in exceptions if isinstance(entry.run, SkippedRun))
        if "finished_runs" in fields:
            finished = _read_run_numbers(fields["finished_runs"], "finished")
        else:
            if run_number_offset > runs_used:
                raise ListError("invalid schedule run history")
            finished = frozenset(range(1, runs_used + 1)) - skipped
            runs_used = len(finished | skipped)
        following_runs = (RetainedFollowingRuns(_read_run_numbers(fields["following_runs"], "following"))
                          if "following_runs" in fields else UseRuleEnd.RULE)
        schedule = ScheduledSeries(first, repeat_rule.parse(_field_text(fields["repeat"], "repeat")), run_allowance,
                                   run_number_offset, tuple(exceptions), runs_used, finished, following_runs)
    _validate_schedule(schedule)
    return schedule


def _run_delivery_fields(delivery: RunDelivery) -> dict[str, object]:
    confirmation = delivery.confirmation
    return {"key": delivery.key, "start": _schedule_time_text(delivery.run.start), "do": delivery.run.words,
            "title": delivery.title, "owner": delivery.owner, "producer": delivery.producer,
            "log_index": delivery.log_index,
            "confirmation": "waiting for confirmation" if isinstance(confirmation, AwaitingConfirmation) else confirmation,
            "confirm_by": _time_text(confirmation.deadline) if isinstance(confirmation, AwaitingConfirmation) else "",
            "notices": [[notice.kind, notice.state] for notice in delivery.notices]}


def _read_run_delivery(raw: object) -> RunDelivery:
    fields = _json_fields(raw)
    index = fields.get("log_index")
    if type(index) is not int:
        raise ListError("invalid scheduled run log reference")
    confirm_by = _field_text(fields.get("confirm_by"), "confirm_by")
    if "confirmation" in fields:
        rows = fields.get("notices")
        if not isinstance(rows, list):
            raise ListError("invalid scheduled run notices")
        notices: list[ScheduledNotice] = []
        for row in cast(list[object], rows):
            match row:
                case [str(kind), str(state)]:
                    notices.append(ScheduledNotice(RunNotice(kind), NoticeState(state)))
                case _:
                    raise ListError("invalid scheduled run notice")
        name = _field_text(fields["confirmation"], "confirmation")
        if name == "waiting for confirmation":
            confirmation: RunConfirmation = AwaitingConfirmation(_required_time(confirm_by))
        else:
            if confirm_by:
                raise ListError("unexpected scheduled run confirmation deadline")
            confirmation = AwaitingInstruction.OWNER if name == AwaitingInstruction.OWNER else RunResult(name)
    else:
        # An earlier delivery can have attempted a send without saving acceptance.
        pending = fields.get("pending")
        if not isinstance(pending, list):
            raise ListError("invalid pending scheduled run notices")
        outstanding = tuple(RunNotice(_field_text(kind, "pending notice")) for kind in cast(list[object], pending))
        result = _field_text(fields.get("result"), "result")
        if result == "waiting":
            confirmation = (AwaitingInstruction.OWNER if RunNotice.OWNER_START in outstanding
                            else AwaitingConfirmation(_required_time(confirm_by)))
        else:
            confirmation = RunResult(result)
        kinds = (*_START_NOTICES, *_TIMEOUT_NOTICES) if confirmation is RunResult.NOT_CONFIRMED else _START_NOTICES
        notices = [ScheduledNotice(kind, NoticeState.SENDING if kind in outstanding else NoticeState.ACCEPTED)
                   for kind in kinds]
    return RunDelivery(_field_text(fields.get("key"), "key"),
                       ScheduledRun(_scheduled_time(_field_text(fields.get("start"), "start")),
                                    _field_text(fields.get("do"), "do")),
                       _field_text(fields.get("title"), "title"),
                       staff_id(_field_text(fields.get("owner"), "owner"), "run owner"),
                       staff_id(_field_text(fields.get("producer"), "producer"), "run producer"),
                       index, confirmation, tuple(notices))


def _read_run_deliveries(fields: dict[str, str]) -> tuple[RunDelivery, ...]:
    if "run_deliveries" not in fields:
        legacy = cast(object, json.loads(fields.get("run_delivery", "null")))
        return (_read_run_delivery(legacy),) if legacy is not None else ()
    rows = cast(object, json.loads(fields["run_deliveries"]))
    if not isinstance(rows, list):
        raise ListError("invalid scheduled run deliveries")
    return tuple(_read_run_delivery(row) for row in cast(list[object], rows))


def _validate_scheduled_run(run: ScheduledRun) -> None:
    _ = _validate_schedule_time(run.start)
    if not _field_text(run.words, "do").strip():
        raise ListError("a Scheduled item needs words for At that time, do this")


def _validate_schedule(schedule: object) -> None:
    if isinstance(schedule, NoSchedule):
        return
    if isinstance(schedule, ScheduledRun):
        _validate_scheduled_run(schedule)
        return
    if not isinstance(schedule, ScheduledSeries):
        raise ListError("invalid List field: schedule")
    _validate_scheduled_run(schedule.first)
    if type(schedule.run_allowance) is not int or schedule.run_allowance < 1:
        raise ListError("invalid schedule run allowance")
    skipped = schedule.skipped_runs
    if (type(schedule.run_number_offset) is not int or schedule.run_number_offset < 0 or type(schedule.runs_used) is not int
            or any(type(number) is not int or number < 1 for number in schedule.finished_runs)
            or schedule.finished_runs & skipped or schedule.runs_used != len(schedule.finished_runs | skipped)):
        raise ListError("invalid schedule run progress")
    if repeat_rule.parse(schedule.rule.rrule) != schedule.rule:
        raise ListError("invalid schedule repeat rule")
    rule = schedule.rule
    retained = schedule.following_runs
    if isinstance(retained, RetainedFollowingRuns):
        if (not retained.numbers
                or any(type(number) is not int or not 0 < number - schedule.run_number_offset <= repeat_rule.MAX_RUN_DATES
                       for number in retained.numbers)):
            raise ListError("invalid following run selection")
        rule = replace(rule, end=repeat_rule.RepeatUntilLimit.REACHED)
    rule.validate_start(schedule.first.start)
    seen: set[int] = set()
    for entry in schedule.exceptions:
        if type(entry.number) is not int or entry.number < 1 or entry.number in seen:
            raise ListError("a schedule exception must name a distinct run number")
        seen.add(entry.number)
        if isinstance(entry.run, ScheduledRun):
            if entry.number <= schedule.run_number_offset:
                raise ListError("an override must name a run in the current series")
            _validate_scheduled_run(entry.run)
        else:
            _ = _field_text(entry.run.words, "skip words")


def series_run_limit() -> int:
    settings = conf.read("the_list")
    limit = settings.origin("series_run_limit", conf.positive_whole)
    if not isinstance(limit, conf.UsableLiveValue):
        raise ListError(f"a repeat needs a valid series_run_limit in {settings.source}")
    return limit.value


def _check_series_limit(schedule: Schedule) -> None:
    if not isinstance(schedule, ScheduledSeries):
        return
    limit = schedule.run_allowance
    if schedule.runs_used >= limit:
        raise ListError(f"the series has no runs left under the limit of {limit}")
    end = schedule.rule.end
    if isinstance(end, repeat_rule.RunCount) and schedule.pending_count(end) > limit - schedule.runs_used:
        raise ListError(f"COUNT exceeds the series limit of {limit} runs")
    if isinstance(end, repeat_rule.RunCount) or schedule.exceptions or isinstance(schedule.following_runs, RetainedFollowingRuns):
        schedule.validate_run_times()


def parking_date(moment: datetime, zone: tzinfo) -> str:
    return moment.astimezone(zone).date().isoformat()


def list_time(moment: datetime, today: datetime) -> str:
    local = moment.astimezone(today.tzinfo)
    return local.strftime("%H:%M %Z" if local.date() == today.date() else "%Y-%m-%d %H:%M %Z")


def _time_text(value: Timestamp) -> str:
    return value.isoformat() if isinstance(value, datetime) else ""


ISO_TIME = re.compile(r"\d{4}-\d{2}-\d{2}(?:T[0-9:.+Z-]+)?")


def next_check_value(text: str) -> NextCheck:
    if not text or ISO_TIME.fullmatch(text):
        return time_value(text)
    _line(text, "next check")
    return CheckCondition(text)


def next_check_text(value: NextCheck) -> str:
    return value.text if isinstance(value, CheckCondition) else _time_text(value)


def eta_value(text: str) -> Eta:
    if text in EtaStatus:
        return EtaStatus(text)
    parts = text.split("/")
    if len(parts) not in (1, 2) or not all(ISO_TIME.fullmatch(part) for part in parts):
        _line(text, "ETA")
        return EtaDescription(text)
    if len(parts) == 1:
        return _required_time(text)
    earliest, latest = (_required_time(part) for part in parts)
    if earliest > latest:
        raise ListError("the ETA range ends before it starts")
    return EtaRange(earliest, latest)


_ETA_INPUT_STATUSES = (EtaStatus.NO_TIME_YET, EtaStatus.WAITING_FOR_NATE, EtaStatus.PAUSED, EtaStatus.DELIVERED)
_ETA_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_ETA_DATE = re.compile(r"([A-Z][a-z]{2}) ([0-9]{1,2})(?: ([01][0-9]|2[0-3]):([0-5][0-9]) (\S+))?")
_ETA_INPUT_ERROR = ("Use --eta with Mon D, Mon D HH:MM ZONE, No time yet, Waiting for nate, Paused, or Delivered. "
                    + "For other details, put the reason in --latest-status.")


def _cli_eta(text: str) -> datetime | date | EtaStatus:
    if text in _ETA_INPUT_STATUSES:
        return EtaStatus(text)
    match = _ETA_DATE.fullmatch(text)
    if match is None or match[1] not in _ETA_MONTHS:
        raise ListError(_ETA_INPUT_ERROR)
    month, day = _ETA_MONTHS.index(match[1]) + 1, int(match[2])
    zone = user_zone()
    today = now().astimezone(zone).date()
    dates: list[date] = []
    # Four years on each side also cover the nearest leap day.
    for year in range(max(date.min.year, today.year - 4), min(date.max.year, today.year + 4) + 1):
        try:
            dates.append(date(year, month, day))
        except ValueError:
            continue
    if not dates:
        raise ListError(_ETA_INPUT_ERROR)
    nearest = min(dates, key=lambda candidate: abs(candidate - today))
    if match[5] is None:
        return nearest
    moment = datetime(nearest.year, month, day, int(match[3]), int(match[4]), tzinfo=zone)
    for fold in (0, 1):
        local = moment.replace(fold=fold)
        if local.astimezone(UTC).astimezone(zone) == local and zone_label(local) == match[5]:
            return local
    raise ListError(_ETA_INPUT_ERROR)


def eta_text(value: Eta) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, EtaRange):
        return f"{value.earliest.isoformat()}/{value.latest.isoformat()}"
    return value.text if isinstance(value, EtaDescription) else value.value


def eta_due(eta: Eta) -> datetime | None:
    if isinstance(eta, datetime):
        return eta
    if isinstance(eta, date) and eta < date.max:
        return datetime.combine(eta + timedelta(days=1), time.min, tzinfo=user_zone())
    if isinstance(eta, EtaRange):
        return eta.latest
    return None


def _eta_kind(eta: Eta) -> Literal["text", "time or status"]:
    return "text" if isinstance(eta, EtaDescription) else "time or status"


def _read_eta(text: str, kind: str) -> Eta:
    if kind == "text":
        _line(text, "ETA")
        return EtaDescription(text)
    if kind == "time or status":
        if len(text) == 10 and ISO_TIME.fullmatch(text):
            return date.fromisoformat(text)
        eta = eta_value(text)
        if not isinstance(eta, EtaDescription):
            return eta
    raise ListError("the ETA value must match its kind")


def _line(value: str, name: str) -> None:
    if not value.strip() or value.splitlines() != [value]:
        raise ListError(f"{name} must have one nonempty line")


def _log_line(value: str, name: str) -> None:
    if not value.strip() or "\n" in value or "\r" in value:
        raise ListError(f"{name} must have one nonempty line")


_SOURCE_CLOCK = re.compile(r"(?:^|(?<=[\s,]))(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d(?:[.,]\d+)?)?"
                           + r"(?: [A-Z]{2,6}|Z| ?[+-]\d{2}(?::?\d{2})?)?$")


def source_without_clock(source: str) -> str:
    """Remove a hand-written clock time from the end of a log source."""
    text = source.rstrip()
    for boundary in re.finditer(r"^|[ \t,]", text):
        start = boundary.end()
        suffix = text[start:]
        try:
            _ = datetime.fromisoformat(suffix)
        except ValueError:
            continue
        try:
            _ = date.fromisoformat(suffix)
        except ValueError:
            return text[:start].rstrip(" ,\t")

    clock = _SOURCE_CLOCK.search(text)
    return text[:clock.start()].rstrip(" ,\t") if clock else source


def staff_id(value: str, name: str) -> staff.StaffId:
    """Read a staff ID in a List record."""
    if not value or Path(value).name != value or value in (".", ".."):
        raise ListError(f"invalid {name} staff id")
    return staff.StaffId(value)


def _field_text(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ListError(f"invalid List field: {name}")
    return value


def _item_state(value: str) -> ItemState:
    if value not in ItemState:
        raise ListError("invalid item state")
    return ItemState(value)


def _requester(value: str) -> RequestedBy:
    if value not in RequestedBy:
        raise ListError("invalid requester")
    return RequestedBy(value)


def _validate_unit(value: str) -> None:
    if value and value.splitlines() != [value]:
        raise ListError("unit must have one line")


def _validate_time(value: Timestamp) -> None:
    if isinstance(value, datetime) and value.utcoffset() is None:
        raise ListError("a time must have a time zone")


def _validate_closing_words(value: str) -> None:
    if not value.strip():
        raise ListError("closing words must not be empty")


def _validate_dependencies(entries: tuple[ItemDependency, ...], number: int) -> None:
    for dependency in entries:
        if dependency.number < 1 or dependency.number == number:
            raise ListError("an item cannot depend on itself or a nonpositive number")
        _line(dependency.condition, "dependency condition")
    if len({entry.number for entry in entries}) != len(entries):
        raise ListError("a dependency number must occur only once")


def _validate_related(entries: tuple[int, ...], number: int) -> None:
    if any(entry < 1 or entry == number for entry in entries):
        raise ListError("a related number must name another positive item")


def _validate_review_keys(keys: tuple[str, ...]) -> None:
    if len(set(keys)) != len(keys):
        raise ListError("a review key must occur only once")
    for key in keys:
        _line(key, "review key")


def _validate_ask(ask: Ask) -> None:
    if ASK_ID.fullmatch(ask.ask_id) is None or ask.kind not in AskKind:
        raise ListError("invalid List ask")
    _line(ask.what, "ask what")
    _line(ask.why, "ask why")
    _ = staff_id(ask.target, "ask target")
    for moment in (ask.opened, ask.closed):
        _validate_time(moment)
    if ask.withdrawn and ask.is_open:
        raise ListError("a withdrawn ask needs a close time")
    if bool(ask.go_id) != (not ask.is_open and not ask.withdrawn) or any(char.isspace() for char in ask.go_id):
        raise ListError("only a closed ask with a Go has a go id, without spaces")


def _validate_asks(asks: tuple[Ask, ...]) -> None:
    for ask in asks:
        _validate_ask(ask)
    if len({ask.ask_id for ask in asks}) != len(asks):
        raise ListError("an ask id must occur only once")


def _stored_ask(ask: Ask) -> dict[str, object]:
    fields: dict[str, object] = {
        "ask_id": ask.ask_id, "kind": ask.kind.value, "what": ask.what, "why": ask.why, "target": ask.target,
        "config": ask.config, "opened": ask.opened.isoformat(), "closed": _time_text(ask.closed), "go_id": ask.go_id,
    }
    if ask.withdrawn:
        fields["withdrawn"] = True
    return fields


_ASK_FIELDS: frozenset[str] = frozenset(("ask_id", "kind", "what", "why", "target", "config", "opened", "closed", "go_id"))


def _read_ask(value: object) -> Ask:
    fields = _json_fields(value)
    withdrawn = fields.get("withdrawn") is True
    expected = _ASK_FIELDS | {"withdrawn"} if withdrawn else _ASK_FIELDS
    if fields.keys() != expected:
        raise ListError("invalid List ask")
    texts = {name: fields[name] for name in _ASK_FIELDS if name != "config"}
    if not all(isinstance(text, str) for text in texts.values()) or not isinstance(fields["config"], bool):
        raise ListError("invalid List ask")
    text = cast(dict[str, str], texts)
    if text["kind"] not in AskKind:
        raise ListError("invalid List ask kind")
    ask = Ask(text["ask_id"], AskKind(text["kind"]), text["what"], text["why"], staff.StaffId(text["target"]),
              fields["config"], _required_time(text["opened"]), time_value(text["closed"]), text["go_id"], withdrawn)
    _validate_ask(ask)
    return ask


def _read_asks(text: str) -> tuple[Ask, ...]:
    value = cast(object, json.loads(text))
    if not isinstance(value, list):
        raise ListError("asks must be a list")
    asks = tuple(_read_ask(entry) for entry in cast(list[object], value))
    _validate_asks(asks)
    return asks


def open_asks(item: ListItem) -> tuple[Ask, ...]:
    return tuple(ask for ask in item.asks if ask.is_open)


def _validate(item: ListItem) -> None:
    if item.number < 1:
        raise ListError("number must be positive")
    _line(item.title, "title")
    _ = staff_id(item.owner, "owner")
    _ = staff_id(item.producer, "producer")
    _validate_unit(item.unit)
    _ = _item_state(item.state)
    _ = _requester(item.requested_by)
    for moment in (item.opened, item.updated, item.unread_since, item.closed):
        _validate_time(moment)
    if not isinstance(item.parked_until, ParkUntilDelivered):
        _validate_time(item.parked_until)
    _ = _read_eta(eta_text(item.eta), _eta_kind(item.eta))
    _ = next_check_value(next_check_text(item.next_check))
    if (item.state == ItemState.CLOSED) != isinstance(item.closed, datetime):
        raise ListError("only a Closed item must have a closed time")
    _validate_schedule(item.schedule)
    if item.state is ItemState.SCHEDULED and item.schedule is NoSchedule.UNSET:
        raise ListError("a schedule requires the Scheduled state and a start and words")
    if (item.state is not ItemState.SCHEDULED and item.schedule is not NoSchedule.UNSET
            and not (item.state is not ItemState.CLOSED and isinstance(item.schedule, ScheduledSeries)
                     and (item.schedule.finished_runs or item.run_deliveries))):
        raise ListError("a schedule requires the Scheduled state and a start and words")
    if item.schedule is not NoSchedule.UNSET and item.parked_until is not NoTimestamp.UNSET:
        raise ListError("a Scheduled item cannot be parked")
    if item.state == ItemState.CLOSED:
        _validate_closing_words(item.closing_words)
    elif item.closing_words:
        raise ListError("only a Closed item can have closing words")
    _validate_dependencies(item.blocked_by, item.number)
    _validate_related(item.related_to, item.number)
    _validate_review_keys(item.reviews)
    _validate_asks(item.asks)
    if isinstance(item.report, ListReport):
        _line(item.report.evidence_key, "report evidence key")
        if not item.report.words.strip():
            raise ListError("a List report needs the original words")
        start = item.report.validation_log
        if isinstance(start, ValidationLogStart) and (
                item.report.kind is not ReportKind.VALIDATION or item.report.submission_scope
                or start.first_result_index < 0):
            raise ListError("invalid validation log position")
    if item.state == ItemState.REPAIRED:
        if not isinstance(item.report, ListReport):
            raise ListError("only a List report can be Repaired")
        _line(item.next_step, "how to try the repair")
    for name, value in (("next_step", item.next_step), ("latest_status", item.latest_status),
                        ("evidence", item.evidence), ("done_when", item.done_when)):
        _ = _field_text(value, name)
    seen_runs: set[str] = set()
    seen_logs: set[int] = set()
    for delivery in item.run_deliveries:
        _validate_scheduled_run(delivery.run)
        _line(delivery.title, "scheduled run title")
        confirmation = delivery.confirmation
        if isinstance(confirmation, AwaitingConfirmation):
            _validate_time(confirmation.deadline)
        kinds = (*_START_NOTICES, *_TIMEOUT_NOTICES) if confirmation is RunResult.NOT_CONFIRMED else _START_NOTICES
        if (len(delivery.notices) != len(kinds) or {notice.kind for notice in delivery.notices} != set(kinds)
                or not re.fullmatch("[a-f0-9]{32}", delivery.key)
                or delivery.key in seen_runs or delivery.log_index in seen_logs
                or not 0 <= delivery.log_index < len(item.log)
                or item.log[delivery.log_index].what != delivery.log_text()):
            raise ListError("invalid scheduled run delivery or log reference")
        owner = next(notice for notice in delivery.notices if notice.kind is RunNotice.OWNER_START)
        if (confirmation is AwaitingInstruction.OWNER and not owner.needs_check()
                or isinstance(confirmation, AwaitingConfirmation) and owner.needs_check()):
            raise ListError("invalid owner instruction state for confirmation")
        seen_runs.add(delivery.key)
        seen_logs.add(delivery.log_index)
    for change in item.log:
        if isinstance(change, _ImageAnswerChange):
            if not change.what.strip() or change.source != _IMAGE_ANSWER_SOURCE:
                raise ListError("invalid image answer log")
        elif isinstance(change.what, str):
            _log_line(change.what, "log text")
        if isinstance(change.what, (PageFeedback, PageRequest, QuestionRequestReceipt, UnparkNoChangeReceipt)):
            _validate_page_source(change.source)
        _log_line(change.source, "source")
        if " — " in change.source or change.time.utcoffset() is None:
            raise ListError("a log source cannot contain its separator; its time must have a time zone")


def _dependencies(value: object) -> tuple[ItemDependency, ...]:
    if not isinstance(value, list):
        raise ListError("blocked_by must be a list")
    entries: list[ItemDependency] = []
    for row in cast(list[object], value):
        if not isinstance(row, list):
            raise ListError("invalid dependency")
        cells = cast(list[object], row)
        if len(cells) != 2 or type(cells[0]) is not int or not isinstance(cells[1], str):
            raise ListError("invalid dependency")
        entries.append(ItemDependency(cells[0], cells[1]))
    return tuple(entries)


def _related(value: object) -> tuple[int, ...]:
    if not isinstance(value, list):
        raise ListError("related_to must be a list")
    entries = cast(list[object], value)
    if any(type(number) is not int for number in entries):
        raise ListError("related_to must contain item numbers")
    return tuple(cast(list[int], entries))


def _review_keys(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ListError("reviews must be a list of keys")
    entries = cast(list[object], value)
    if any(not isinstance(entry, str) for entry in entries):
        raise ListError("reviews must be a list of keys")
    return tuple(cast(list[str], entries))


def _log_fields(fields: LogFields) -> dict[str, object]:
    values: dict[str, object] = dict(fields)
    if "schedule" in fields:
        values["schedule"] = _schedule_fields(fields["schedule"])
    if "producer" in fields:
        values["producer"] = staff_id(fields["producer"], "producer")
    if "eta" in fields:
        eta = fields["eta"]
        values["eta"] = {"value": eta_text(eta), "kind": _eta_kind(eta)}
    if "next_check" in fields:
        values["next_check"] = next_check_text(fields["next_check"])
    if "parked_until" in fields:
        values["parked_until"] = question_path.write_later(later_value(fields["parked_until"]))
    if "blocked_by" in fields:
        values["blocked_by"] = [(dependency.number, dependency.condition) for dependency in fields["blocked_by"]]
    return values


def _json_fields(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ListError("a log object must contain named fields")
    return cast(dict[str, object], value)


def _read_log_fields(raw: object, number: int, read_schedule: Callable[[object], Schedule]) -> LogFields:
    values: dict[str, object] = {}
    for field, value in _json_fields(raw).items():
        if field not in LogFields.__annotations__:
            raise ListError(f"unknown List log field: {field}")
        if field == "schedule":
            values[field] = read_schedule(value)
        elif field == "eta":
            eta = _json_fields(value)
            text, kind = eta.get("value"), eta.get("kind")
            if not isinstance(text, str) or not isinstance(kind, str):
                raise ListError("invalid log ETA")
            values[field] = _read_eta(text, kind)
        elif field == "blocked_by":
            dependencies = _dependencies(value)
            _validate_dependencies(dependencies, number)
            values[field] = dependencies
        elif field == "related_to":
            related = _related(value)
            _validate_related(related, number)
            values[field] = related
        else:
            text = _field_text(value, field)
            match field:
                case "owner":
                    identity = staff_id(text, field)
                    values[field] = UnassignedOwner(text) if text in UnassignedOwner else identity
                case "producer":
                    values[field] = staff_id(text, field)
                case "state":
                    values[field] = _item_state(text)
                case "requested_by":
                    values[field] = _requester(text)
                case "next_check":
                    values[field] = next_check_value(text)
                case "parked_until":
                    values[field] = parking_value(text)
                case _:
                    if field == "title":
                        _line(text, field)
                    elif field == "unit":
                        _validate_unit(text)
                    elif field == "closing_words":
                        _validate_closing_words(text)
                    values[field] = text
    return cast(LogFields, cast(object, values))


_LEGACY_CONSTRUCTORS: dict[str, Callable[..., object]] = {
    "EtaDescription": EtaDescription, "EtaRange": EtaRange, "CheckCondition": CheckCondition,
    "ItemDependency": ItemDependency, "datetime.datetime": datetime, "datetime.timedelta": timedelta,
    "datetime.timezone": timezone, "zoneinfo.ZoneInfo": ZoneInfo,
}
_LEGACY_FIELD = re.compile(r"([a-z_]+): ")


def _legacy_repr(node: ast.expr) -> object:
    if isinstance(node, ast.Tuple):
        return tuple(_legacy_repr(value) for value in node.elts)
    if isinstance(node, ast.Attribute) and ast.unparse(node) == "datetime.timezone.utc":
        return UTC
    if isinstance(node, ast.Call):
        constructor = _LEGACY_CONSTRUCTORS[ast.unparse(node.func)]
        kwargs: dict[str, object] = {}
        for keyword in node.keywords:
            if keyword.arg is None or keyword.arg in kwargs:
                raise ListError("invalid arguments in old List log")
            kwargs[keyword.arg] = _legacy_repr(keyword.value)
        return constructor(*(_legacy_repr(arg) for arg in node.args), **kwargs)
    return cast(object, ast.literal_eval(node))


def _legacy_log_field(field: str, text: str) -> object:
    if field == "eta":
        if text.startswith(("EtaRange(", "EtaDescription(")):
            eta = _legacy_repr(ast.parse(text, mode="eval").body)
            if isinstance(eta, (EtaRange, EtaDescription)):
                return _log_fields({"eta": eta})["eta"]
            raise ListError("invalid ETA in old List log")
        candidate = text.replace(" ", "T", 1)
        eta = eta_value(candidate if ISO_TIME.fullmatch(candidate) else text)
        return _log_fields({"eta": eta})["eta"]
    if field == "next_check":
        if text.startswith("CheckCondition("):
            check = _legacy_repr(ast.parse(text, mode="eval").body)
            if isinstance(check, CheckCondition):
                return check.text
            raise ListError("invalid next check in old List log")
        candidate = text.replace(" ", "T", 1)
        return candidate if ISO_TIME.fullmatch(candidate) else text
    if field in ("blocked_by", "related_to"):
        value = _legacy_repr(ast.parse(text, mode="eval").body)
        if isinstance(value, tuple):
            parts = cast(tuple[object, ...], value)
            if field == "related_to":
                return list(parts)
            if all(isinstance(part, ItemDependency) for part in parts):
                return [[part.number, part.condition] for part in parts if isinstance(part, ItemDependency)]
        raise ListError("invalid item links in old List log")
    return text


def _read_legacy_note(note: str, source: str, number: int, read_schedule: Callable[[object], Schedule]) -> LogContent:
    action, _, fields = note.partition(" ")
    try:
        if source == PAGE_SOURCE and note.startswith((_LEGACY_FEEDBACK, _LEGACY_RECEIPT)):
            receipt = note.startswith(_LEGACY_RECEIPT)
            prefix = _LEGACY_RECEIPT if receipt else _LEGACY_FEEDBACK
            activity = _json_fields(cast(object, json.loads(note.removeprefix(prefix))))
            if not receipt:
                _ = activity.setdefault("action", "tell")
            return _read_page_activity(activity, source, receipt=receipt)
        if note.startswith("Parked until "):
            date = note.removeprefix("Parked until ")
            until = NoTimestamp.UNSET if date == "not parked" else _required_time(date)
            return LegacyFieldChange("Parked", {"parked_until": until}, note)
        if action == "Closed:" and fields.strip():
            return LegacyFieldChange("Closed", {"closing_words": fields}, note)
    except ValueError:
        return note
    if action not in ("Added;", "Updated"):
        return note
    decoder = json.JSONDecoder()
    values: dict[str, object] = {}
    offset = 0
    try:
        while True:
            match = _LEGACY_FIELD.match(fields, offset)
            if match is None:
                return note
            field = match[1]
            text, end = cast(tuple[object, int], decoder.raw_decode(fields, match.end()))
            if not isinstance(text, str) or field in values:
                return note
            values[field] = _legacy_log_field(field, text)
            if end == len(fields):
                return LegacyFieldChange("Added" if action == "Added;" else "Updated", _read_log_fields(values, number, read_schedule), note)
            if not fields.startswith("; ", end):
                return note
            offset = end + 2
    except (AttributeError, KeyError, OverflowError, RecursionError, SyntaxError, TypeError, ValueError):
        return note


def _request_id(value: object) -> str:
    if not isinstance(value, str) or not value or any(char.isspace() for char in value):
        raise ListError("enter a request ID without spaces")
    return value


def _question_id(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"Q[1-9][0-9]*", value) is None:
        raise ListError("invalid List question ID")
    return value


def _request_fields(request: PageRequest) -> dict[str, str]:
    fields = {"request": _request_id(request.request), "action": request.action, "words": request.words, "form_words": request.form_words}
    if request.parked_until is not NoTimestamp.UNSET:
        fields["parked_until"] = question_path.write_later(later_value(request.parked_until))
    return fields


def _log_content(content: LogContent) -> dict[str, object]:
    if isinstance(content, str):
        return {"note": content}
    if isinstance(content, LegacyFieldChange):
        return {"note": content.note}
    if isinstance(content, PageParkingChange):
        return {"action": content.action, "fields": _log_fields(content.fields), "request": _request_id(content.request)}
    if isinstance(content, FieldChange):
        return {"action": content.action, "fields": _log_fields(content.fields)}
    if isinstance(content, QuestionAnswered):
        return {"action": "Answered", "question_id": _question_id(content.question_id)}
    if isinstance(content, ReturnedAnswer):
        return {"action": "Returned answer", "source_key": content.identity.source_key,
                "exchange_id": content.identity.exchange_id, "words": content.words}
    if isinstance(content, QuestionClosed):
        return {"action": "Question closed with item", "question": question_record.stored(content.question)}
    if isinstance(content, MarkedRead):
        return {"action": "Marked read"}
    if isinstance(content, Reopened):
        return {"action": "Reopened"}
    if isinstance(content, UnparkedForDelivery):
        if isinstance(content.reason, LinkedChangeDelivered):
            # Preserve old entries when saving the item.
            return {"action": "Unparked", "delivered_change": content.reason.key}
        return {"action": "Unparked", "delivered_item": True}
    if isinstance(content, Unparked):
        return {"action": "Unparked"}
    if isinstance(content, ReviewLinksChanged):
        return {"action": content.action, "keys": content.keys}
    if isinstance(content, EvidenceCopyRemoved):
        return {"action": "Removed Evidence copy", "copy": content.copy}
    if isinstance(content, AskOpened):
        return {"action": "Ask opened", "ask": _stored_ask(content.ask)}
    if isinstance(content, AskWithdrawn):
        return {"action": "Ask withdrawn", "ask": _stored_ask(content.ask), "reason": content.reason}
    if isinstance(content, GoGiven):
        return {"action": "Go given", "ask": _stored_ask(content.ask)}
    if isinstance(content, GoNotDelivered):
        return {"action": "Go not delivered", "ask": _stored_ask(content.ask), "recipient": content.recipient}
    if isinstance(content, PageFeedback):
        fields = _request_fields(content.key())
        fields.update(state=content.state, alerted_to=content.alerted_to)
        if isinstance(content.read_update, UnreadUpdate):
            fields["read_update"] = content.read_update.fingerprint
        if content.recipient is not LegacyFeedbackRecipient.KEEPER:
            fields["recipient"] = staff_id(content.recipient, "feedback recipient")
        if content.additional_recipient is not NoAdditionalRecipient.ABSENT:
            fields["additional_recipient"] = staff_id(content.additional_recipient, "additional feedback recipient")
        if content.origin is PageNoticeOrigin.STUDIO:
            fields["origin"] = content.origin
        return {"feedback": fields}
    if isinstance(content, QuestionRequestReceipt):
        return {"receipt": {**_request_fields(content.request), "question_id": _question_id(content.question_id)}}
    if isinstance(content, UnparkNoChangeReceipt):
        return {"receipt": {**_request_fields(content.request), "unpark_no_change": content.reason.value}}
    return {"receipt": _request_fields(content)}


def _read_field_change(action: Literal["Added", "Updated", "Parked", "Closed"], raw: object, number: int,
                       read_schedule: Callable[[object], Schedule]) -> FieldChange:
    fields = _read_log_fields(raw, number, read_schedule)
    match action:
        case "Added":
            valid = {"title", "owner", "producer", "state", "eta", "next_step", "latest_status"} <= fields.keys()
            valid = valid and fields.keys() <= ItemChanges.__annotations__.keys()
        case "Updated":
            allowed = ItemChanges.__annotations__.keys() | ({"parked_until"} if "schedule" in fields else set[str]())
            valid = bool(fields) and fields.keys() <= allowed
        case "Parked":
            valid = fields.keys() in ({"parked_until"}, {"parked_until", "schedule", "state"})
        case "Closed":
            valid = fields.keys() in ({"closing_words"}, {"closing_words", "schedule"})
    if not valid:
        raise ListError(f"invalid fields in a {action} log entry")
    return FieldChange(action, fields)


def _read_log_line(line: str, number: int, read_schedule: Callable[[object], Schedule]) -> ItemChange:
    if line.startswith("- {"):
        fields = _json_fields(cast(object, json.loads(line[2:])))
        time, source = fields.pop("time"), fields.pop("source")
        if not isinstance(time, str) or not isinstance(source, str):
            raise ListError("invalid log time or source")
        content: LogContent
        match fields:
            case {"note": str(note)} if len(fields) == 1:
                content = _read_legacy_note(note, source, number, read_schedule)
            case {"note": str(note), "image_answer": {"key": str(key), "producer": str(producer)}} if len(fields) == 2:
                return _ImageAnswerChange(_required_time(time), note, source, _request_id(key),
                                          staff_id(producer, "image answer producer"))
            case {"action": "Parked", "fields": values, "request": request} if len(fields) == 3:
                parking = _read_field_change("Parked", values, number, read_schedule)
                content = PageParkingChange("Parked", parking.fields, _request_id(request))
            case {"action": action, "fields": values} if len(fields) == 2 and action in ("Added", "Updated", "Parked", "Closed"):
                content = _read_field_change(action, values, number, read_schedule)
            case {"action": "Answered", "question_id": question_id} if len(fields) == 2:
                content = QuestionAnswered(_question_id(question_id))
            case {"action": "Returned answer", "source_key": str(key), "exchange_id": str(exchange_id),
                  "words": str(words)} if len(fields) == 4:
                _log_line(key, "exchange source key")
                _log_line(exchange_id, "exchange ID")
                content = ReturnedAnswer(ReturnedAnswerId(key, exchange_id), words)
            case {"action": "Question closed with item", "question": value} if len(fields) == 2:
                question = question_record.read(value)
                if question.answer is not None:
                    raise ListError("a question closed with the item has no answer")
                content = QuestionClosed(question)
            case {"action": "Marked read"} if len(fields) == 1:
                content = MarkedRead()
            case {"action": "Reopened"} if len(fields) == 1:
                content = Reopened()
            case {"action": "Unparked", "delivered_change": str(key)} if len(fields) == 2:
                _line(key, "review key")
                content = UnparkedForDelivery(LinkedChangeDelivered(key))
            case {"action": "Unparked", "delivered_item": True} if len(fields) == 2:
                content = UnparkedForDelivery(ItemDelivered.RECORDED)
            case {"action": "Unparked"} if len(fields) == 1:
                content = Unparked()
            case {"action": action, "keys": values} if len(fields) == 2 and action in ("Linked reviews", "Unlinked reviews"):
                keys = _review_keys(values)
                _validate_review_keys(keys)
                if not keys:
                    raise ListError("a review link event must name a review key")
                content = ReviewLinksChanged(action, keys)
            case {"action": "Removed Evidence copy", "copy": copy} if len(fields) == 2 and copy in ("checkins.json", "the List row"):
                content = EvidenceCopyRemoved(copy)
            case {"action": "Ask opened", "ask": value} if len(fields) == 2:
                ask = _read_ask(value)
                if not ask.is_open:
                    raise ListError("an opened ask has no close time")
                content = AskOpened(ask)
            case {"action": "Ask withdrawn", "ask": value, "reason": str(reason)} if len(fields) == 3:
                ask = _read_ask(value)
                if not ask.withdrawn:
                    raise ListError("an ask withdrawal needs a withdrawn ask")
                _line(reason, "withdrawal reason")
                content = AskWithdrawn(ask, reason)
            case {"action": "Go given", "ask": value} if len(fields) == 2:
                ask = _read_ask(value)
                if not ask.go_id:
                    raise ListError("a Go needs a closed ask with a go id")
                content = GoGiven(ask)
            case {"action": "Go not delivered", "ask": value, "recipient": str(recipient)} if len(fields) == 3:
                ask = _read_ask(value)
                _ = staff_id(recipient, "Go recipient")
                if not ask.go_id:
                    raise ListError("an undelivered Go needs a closed ask with a go id")
                content = GoNotDelivered(ask, staff.StaffId(recipient))
            case {"feedback": feedback} if len(fields) == 1:
                content = _read_page_activity(_json_fields(feedback), source, receipt=False)
            case {"receipt": receipt} if len(fields) == 1:
                content = _read_page_activity(_json_fields(receipt), source, receipt=True)
            case _:
                raise ListError("invalid List log entry")
        return ItemChange(_required_time(time), content, source)
    if not line.startswith("- "):
        raise ListError("invalid log line")
    dated, source = line[2:].rsplit(" — ", 1)
    date, clock, note = dated.split(" ", 2)
    return ItemChange(_required_time(date + "T" + clock), _read_legacy_note(note, source, number, read_schedule), source)


def load(keeper: staff.StaffId, identity: str, *, sources: list[Path] | None = None) -> ListItem:
    store = _store(keeper)
    fields, body = store.read(identity, sources=sources)
    return _read_item(store, identity, fields, body)


def _read_report(value: str) -> ListReport | NoListReport:
    if not value:
        return NoListReport.ABSENT
    data = cast(object, json.loads(value))
    match data:
        case [str(kind), str(key), str(words)]:
            return ListReport(ReportKind(kind), key, words)
        case [str(kind), str(key), str(words), str(scope)] if scope:
            return ListReport(ReportKind(kind), key, words, scope)
        case [ReportKind.VALIDATION, str(key), str(words), int(index)] if type(index) is int:
            return ListReport(ReportKind.VALIDATION, key, words, validation_log=ValidationLogStart(index))
        case _:
            raise ListError("invalid List report")


def _read_item(store: record_store.NumberedRecords, identity: str, fields: dict[str, str], body: str) -> ListItem:
    try:
        marker = "## Next step\n\n"
        if not body.startswith(marker):
            raise ListError("missing Next step section")
        body = body[len(marker):]
        texts: list[str] = []
        for key, name in (("next_step", "Latest status"), ("latest_status", "Evidence"),
                          ("evidence", "Done when"), ("done_when", "Log")):
            text, body = record_store.split_section(body, int(fields[key + "_length"]), name)
            texts.append(text)
        log_lines = body.removesuffix("\n").split("\n") if body else []
        number = int(fields["number"])
        legacy_allowance = cache(series_run_limit)
        schedule = _read_schedule(cast(object, json.loads(fields.get("schedule", "null"))), legacy_allowance)

        def read_log_schedule(raw: object) -> Schedule:
            return _read_schedule(raw, lambda: schedule.run_allowance if isinstance(schedule, ScheduledSeries) else legacy_allowance())

        changes = tuple(_read_log_line(line.removesuffix("\r"), number, read_log_schedule) for line in log_lines)
        item = ListItem(number, fields["title"],
                        UnassignedOwner(fields["owner"]) if fields["owner"] in UnassignedOwner else staff.StaffId(fields["owner"]),
                        staff.StaffId(fields.get("producer", store.owner)),
                        fields["unit"], _item_state(fields["state"]), next_check_value(fields["next_check"]),
                        _read_eta(fields["eta"], fields["eta_kind"]),
                        time_value(fields["unread_since"]), parking_value(fields["parked_until"]),
                        _dependencies(cast(object, json.loads(fields["blocked_by"]))),
                        _related(cast(object, json.loads(fields["related_to"]))), _requester(fields["requested_by"]),
                        time_value(fields["opened"]), time_value(fields["updated"]), time_value(fields["closed"]),
                        fields["closing_words"], texts[0], texts[1], texts[2], texts[3], changes,
                        _review_keys(cast(object, json.loads(fields.get("reviews", "[]")))),
                        delivery_state.read_list_questions(fields.get("questions", "[]")), schedule,
                        _read_run_deliveries(fields), list_draft.read_list_draft(fields),
                        _read_asks(fields.get("asks", "[]")), _read_report(fields.get("report", "")),
                        list_exchange.decode(fields.get("filed_exchanges", "[]")),
                        QuestionItemKind(fields["question_kind"]) if "question_kind" in fields else NoQuestionItem.ORDINARY)
        if item.id != identity or not changes:
            raise ListError("the item number must match its path and the log must not be empty")
        _validate(item)
        return item
    except (ValueError, KeyError, TypeError) as error:
        raise store.invalid(identity, error) from None


def _require_active_open_producers(items: Sequence[ListItem]) -> None:
    producers = dict.fromkeys(item.producer for item in items if item.state != ItemState.CLOSED)
    if not producers:
        return
    try:
        record = staff.authority_snapshot()
    except staff.StaffAbsent:
        return
    for identity in producers:
        producer = record.by_id(identity)
        if producer is not None and isinstance(producer.retirement, staff.RetiredProducer):
            raise RetiredListProducer(f"{producer.name}: retired producer. Choose an active producer.")


def _stored_log_line(entry: ItemChange) -> str:
    fields = _log_content(entry.what)
    if isinstance(entry, _ImageAnswerChange):
        fields["image_answer"] = {"key": entry.key, "producer": entry.producer}
    return "- " + json.dumps({"time": entry.time.isoformat(), "source": entry.source, **fields},
                              ensure_ascii=False)


def _goal_status(item: ListItem) -> goal.ItemStatus:
    eta = "none recorded" if item.eta is EtaStatus.NOT_ASKED else eta_text(item.eta)
    return goal.ItemStatus(item.title, item.state.value, eta)


def _save(store: record_store.NumberedRecords, item: ListItem, *, previous_producer: staff.StaffId | None = None) -> None:
    _validate(item)
    fields = {"id": item.id, "number": str(item.number), "title": item.title,
              "owner": item.owner, "producer": item.producer, "unit": item.unit, "state": item.state.value,
              "next_check": next_check_text(item.next_check), "eta": eta_text(item.eta),
              "eta_kind": _eta_kind(item.eta),
              "unread_since": _time_text(item.unread_since),
              "parked_until": question_path.write_later(later_value(item.parked_until)),
              "blocked_by": json.dumps([(entry.number, entry.condition) for entry in item.blocked_by], ensure_ascii=False),
              "related_to": json.dumps(item.related_to), "reviews": json.dumps(item.reviews),
              "requested_by": item.requested_by.value,
              "opened": _time_text(item.opened), "updated": _time_text(item.updated),
              "closed": _time_text(item.closed), "closing_words": item.closing_words}
    if item.schedule is not NoSchedule.UNSET:
        fields["schedule"] = json.dumps(_schedule_fields(item.schedule), ensure_ascii=False)
    if isinstance(item.new_item_draft_id, list_draft.NewItemDraft):
        fields["new_item_draft_id"] = item.new_item_draft_id.stored()
    check = _scheduled_check(item)
    if isinstance(check, ScheduledCheckAt):
        fields["scheduled_check"] = check.time.isoformat()
    elif check is ScheduledCheckState.READY:
        fields["scheduled_check"] = _SCHEDULED_CHECK_READY
    elif item.schedule is not NoSchedule.UNSET:
        fields["scheduled_check"] = _SCHEDULED_CHECK_IDLE
    if item.run_deliveries:
        fields["run_deliveries"] = json.dumps([_run_delivery_fields(delivery) for delivery in item.run_deliveries],
                                             ensure_ascii=False)
    if item.questions:
        fields["questions"] = json.dumps([question_record.stored(question) for question in item.questions], ensure_ascii=False)
    if item.filed_exchanges:
        fields["filed_exchanges"] = list_exchange.encode(item.filed_exchanges)
    if isinstance(item.question_kind, QuestionItemKind):
        fields["question_kind"] = item.question_kind.value
    if item.asks:
        fields["asks"] = json.dumps([_stored_ask(ask) for ask in item.asks], ensure_ascii=False)
    if isinstance(item.report, ListReport):
        report = item.report
        values: tuple[str | int, ...] = (report.kind, report.evidence_key, report.words)
        if isinstance(report.validation_log, ValidationLogStart):
            values += (report.validation_log.first_result_index,)
        elif report.submission_scope:
            values += (report.submission_scope,)
        fields["report"] = json.dumps(values, ensure_ascii=False)
    body = "## Next step\n\n" + item.next_step
    log = "\n".join(_stored_log_line(entry) for entry in item.log)
    for key, text, heading, following in (("next_step", item.next_step, "Latest status", item.latest_status),
                                          ("latest_status", item.latest_status, "Evidence", item.evidence),
                                          ("evidence", item.evidence, "Done when", item.done_when),
                                          ("done_when", item.done_when, "Log", log)):
        fields[key + "_length"] = str(len(text))
        body += record_store.section(heading) + following
    with goal.list_item_change(store.owner, item.id, item.producer, _goal_status(item), previous_producer):
        store.write(item.id, fields, body)


def records(keeper: staff.StaffId, *, include_closed: bool = False,
            sources: list[Path] | None = None) -> list[ListItem]:
    items = _store(keeper).records(lambda identity: load(keeper, identity), sources=sources)
    return sorted((item for item in items if include_closed or item.state != ItemState.CLOSED), key=lambda item: item.number)


def _producer_records(keeper: staff.StaffId, producer: staff.StaffId, *, include_closed: bool) -> ProducerListRead:
    store = _store(keeper)

    def read_visible(identity: str) -> ListItem | ProducerRecordOmission:
        try:
            fields, body = store.read(identity)
            assigned = staff_id(fields.get("producer", keeper), "producer")
            if assigned != producer:
                return ProducerRecordOmission.OUTSIDE_SCOPE
            return _read_item(store, identity, fields, body)
        except (ListError, OSError):
            return ProducerRecordOmission.UNREADABLE

    try:
        # Request source paths so the directory scan reports read errors.
        results = store.records(read_visible, sources=[])
    except OSError:
        return ProducerListRead([], incomplete=True)
    items = [item for item in results if isinstance(item, ListItem) and (include_closed or item.state != ItemState.CLOSED)]
    return ProducerListRead(items, incomplete=ProducerRecordOmission.UNREADABLE in results)


@dataclass(frozen=True)
class WaitingForAnswer:
    """Each unanswered question reached the producer or has a live or scheduled send."""


@dataclass(frozen=True)
class QuestionAttention:
    failed: frozenset[str] = frozenset()


def question_visibility(keeper: staff.StaffId, item: ListItem,
                        status: change_page.DeliveryStatus) -> WaitingForAnswer | QuestionAttention:
    """Read the published state of each unanswered question."""
    unanswered = tuple(question for question in item.questions if question.answer is None)
    if not unanswered:
        return QuestionAttention()
    waiting = True
    failed: set[str] = set()
    for question in unanswered:
        target = list_question_target(keeper, item, question.recipient_id(keeper))
        delivery = change_page.published_question_delivery_view(status, target.saved(question))
        waiting &= delivery.state.waits
        if delivery.state is delivery_state.QuestionSendState.FAILED:
            failed.add(question.question_id)
    return WaitingForAnswer() if waiting else QuestionAttention(frozenset(failed))


def _check_links(number: int, changes: ItemChanges, *, require_related: Callable[[int], None],
                 read_dependency: Callable[[int], ListItem]) -> None:
    for related in changes.get("related_to", ()):
        require_related(related)
    visited: set[int] = set()

    def visit(target: int) -> None:
        if target == number:
            raise ListError("dependencies must not form a cycle")
        if target in visited:
            return
        visited.add(target)
        for dependency in read_dependency(target).blocked_by:
            visit(dependency.number)

    for dependency in changes.get("blocked_by", ()):
        visit(dependency.number)


def create_records(keeper: staff.StaffId, items: Sequence[ListItem]) -> None:
    """Create complete typed records after checking the whole batch under one lock."""
    store = _store(keeper)
    with _list_allocation(store):
        next_number = store.next_number()
        for item in items:
            if item.number < next_number:
                raise ListError(f"{item.id}: the next available number is L{next_number}")
        if any(store.folder().glob("*.md")):
            raise ListError("the list folder already holds a record")
        if os.path.lexists(store.counter_path()):
            raise ListError("the list folder already has a number sequence")
        pending = {item.number: item for item in items}
        if len(pending) != len(items):
            raise ListError("item numbers must be unique")

        def read(number: int) -> ListItem:
            item = pending.get(number)
            return item if item is not None else load(keeper, f"L{number}")

        def require_related(number: int) -> None:
            _ = read(number)

        for item in items:
            _validate(item)
            if not item.log:
                raise ListError("the log must not be empty")
            if store.path(item.id).exists():
                raise ListError(f"{item.id}: the item already exists")
            _check_links(item.number, {"blocked_by": item.blocked_by, "related_to": item.related_to},
                         require_related=require_related, read_dependency=read)
        _require_active_open_producers(items)
        if pending:
            store.reserve_number(max(pending))
        for item in items:
            _save(store, item)


class _CreationDefaults(TypedDict):
    unit: str
    state: ItemState
    next_check: NextCheck
    eta: Eta
    parked_until: Parking
    blocked_by: tuple[ItemDependency, ...]
    related_to: tuple[int, ...]
    requested_by: RequestedBy
    closing_words: str
    next_step: str
    latest_status: str
    evidence: str
    done_when: str


_CREATION_DEFAULTS: _CreationDefaults = {
    "unit": "", "state": ItemState.NEW, "next_check": NoTimestamp.UNSET, "eta": EtaStatus.NOT_ASKED,
    "parked_until": NoTimestamp.UNSET, "blocked_by": (), "related_to": (), "requested_by": RequestedBy.NATE,
    "closing_words": "", "next_step": "", "latest_status": "", "evidence": "", "done_when": "",
}


def add(keeper: staff.StaffId, title: str, *, owner: ItemOwner, source: str,
        changes: ItemChanges,
        number: int | NextNumber = NextNumber.AVAILABLE, requested_by: RequestedBy = _CREATION_DEFAULTS["requested_by"],
        assigned_to: staff.StaffId | AllItems = AllItems.INCLUDED,
        new_item_draft_id: list_draft.ListDraftOrigin = list_draft.NoNewItemDraft.ABSENT,
        before_save: Callable[[int], None] | None = None,
        ) -> ListItem:
    """Create an item. Call before_save with its reserved number under the allocation lock."""
    store = _store(keeper)

    def read_reference(number: int, *, scope: staff.StaffId | AllItems) -> ListItem:
        identity = f"L{number}"
        if assigned_to is AllItems.INCLUDED:
            return load(keeper, identity)
        try:
            fields, body = store.read(identity)
            if scope is AllItems.INCLUDED or fields.get("producer", keeper) == scope:
                return _read_item(store, identity, fields, body)
        except (ListError, OSError):
            pass
        raise ListError("the referenced List item does not exist")

    def require_related(number: int) -> None:
        _ = read_reference(number, scope=AllItems.INCLUDED)

    with _list_allocation(store):
        next_number = store.next_number()
        chosen = next_number if isinstance(number, NextNumber) else number
        identity = f"L{chosen}"
        if os.path.lexists(store.path(identity)):
            raise ListError(f"{identity}: the item already exists")
        if chosen < next_number:
            raise ListError(f"{identity}: the next available number is L{next_number}")
        item = _new_item(chosen, title, owner, keeper, source, changes, requested_by=requested_by,
                         new_item_draft_id=new_item_draft_id)
        _check_series_limit(item.schedule)
        _check_links(item.number, changes, require_related=require_related,
                     read_dependency=lambda number: read_reference(number, scope=assigned_to))
        _require_active_open_producers((item,))
        store.reserve_number(chosen)
        if before_save is not None:
            before_save(chosen)
        _save(store, item)
        return item


def _new_item(number: int, title: str, owner: ItemOwner, keeper: staff.StaffId, source: str,
              changes: ItemChanges, *, requested_by: RequestedBy = RequestedBy.NATE,
              reviews: tuple[str, ...] = (),
              new_item_draft_id: list_draft.ListDraftOrigin = list_draft.NoNewItemDraft.ABSENT) -> ListItem:
    moment = now()
    defaults = _CREATION_DEFAULTS.copy()
    defaults["requested_by"] = requested_by
    item = ListItem(number, title, owner, keeper, unread_since=moment, opened=moment, updated=moment,
                    closed=NoTimestamp.UNSET, log=(), reviews=reviews,
                    new_item_draft_id=new_item_draft_id, **defaults)
    if changes:
        item = replace(item, **changes)
    initial: LogFields = {"title": item.title, "owner": item.owner, "producer": item.producer,
                          "state": item.state, "eta": item.eta, "next_step": item.next_step,
                          "latest_status": item.latest_status, "requested_by": item.requested_by, **changes}
    log = (ItemChange(moment, FieldChange("Added", initial), source),)
    if reviews:
        log += (ItemChange(moment, ReviewLinksChanged("Linked reviews", reviews), source),)
    item = replace(item, log=log)
    _validate(item)
    return item


def reserved_review_number(path: Path) -> int | NextNumber:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return NextNumber.AVAILABLE
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise ListError(f"{path}: invalid List number reservation")
    return int(value)


def add_review_item(keeper: staff.StaffId, title: str, *, owner: staff.StaffId, producer: staff.StaffId,
                    unit: str, words: str, evidence: str, review_key: str, source: str, reservation: Path) -> int:
    """Reserve a number for this Review filing and publish its List row."""
    if review_key.startswith("investigation/"):
        try:
            record = investigation.list_evidence_record(review_key)
        except (FileNotFoundError, investigation.InvestigationError):
            pass
        else:
            report_kind = investigation_report_kind(record)
            if isinstance(report_kind, ReportKind):
                path = investigation.review_record_path(record.owner, record.id)
                def finish_report(current: investigation.Investigation) -> investigation.ListDestination:
                    report = report_from_investigation(current, report_kind, review_key)
                    number = file_report(keeper, report, current.title, owner=owner, producer=producer, unit=unit,
                                         source=source, reservation=reservation)
                    return investigation.ListDestination(keeper, number)

                record = investigation.file_on_list(path, finish_report)
                assert isinstance(record.list_destination, investigation.ListDestination)
                return record.list_destination.number
    store = _store(keeper)
    number = reserved_review_number(reservation)
    if isinstance(number, int) and store.path(f"L{number}").is_file():
        sync_folder(store.folder())
        return number
    with _list_allocation(store):
        number = reserved_review_number(reservation)
        if isinstance(number, int) and store.path(f"L{number}").is_file():
            sync_folder(store.folder())
            return number
        chosen = store.next_number() if isinstance(number, NextNumber) else number
        item = _new_item(chosen, title, owner, keeper, source,
                         {"producer": producer, "unit": unit, "latest_status": words, "evidence": evidence},
                         reviews=(review_key,))
        _require_active_open_producers((item,))
        if isinstance(number, NextNumber):
            store.reserve_number(chosen)
            write_text_atomic(reservation, f"{chosen}\n", new_file_mode=0o600)
        _save(store, item)
        return chosen


class QuestionFiling(Enum):
    LIVE = "file a current question"
    HISTORY = "migrate a question"


class QuestionMove(Enum):
    FILE = "file"
    NATE_ANSWER = "nate answers"
    WITHDRAW = "withdraw"
    REPLY = "receive a reply"
    READ = "mark read"
    ASKER_CLOSE = "asker closes"
    OTHER = "ordinary item action"


type QuestionTransition = tuple[QuestionItemKind, ItemState, QuestionMove, ItemState]

_QUESTION_TRANSITIONS: frozenset[QuestionTransition] = frozenset({
    (QuestionItemKind.TO_NATE, ItemState.WAITING_FOR_NATE, QuestionMove.NATE_ANSWER, ItemState.WORKING),
    (QuestionItemKind.TO_NATE, ItemState.WAITING_FOR_NATE, QuestionMove.WITHDRAW, ItemState.CLOSED),
    (QuestionItemKind.TO_NATE, ItemState.WORKING, QuestionMove.ASKER_CLOSE, ItemState.CLOSED),
    (QuestionItemKind.TO_NATE, ItemState.ANSWERED, QuestionMove.READ, ItemState.WORKING),
    (QuestionItemKind.FROM_NATE, ItemState.ANSWERED, QuestionMove.READ, ItemState.CLOSED),
    (QuestionItemKind.FROM_NATE, ItemState.CLOSED, QuestionMove.REPLY, ItemState.ANSWERED),
    *((kind, ItemState.WORKING, QuestionMove.REPLY, ItemState.ANSWERED) for kind in QuestionItemKind),
    *((QuestionItemKind.TO_NATE, state, QuestionMove.FILE, target)
      for state in (ItemState.NEW, ItemState.WAITING_FOR_NATE, ItemState.WORKING, ItemState.ANSWERED)
      for target in (ItemState.WAITING_FOR_NATE, ItemState.WORKING, ItemState.CLOSED)),
    *((QuestionItemKind.FROM_NATE, state, QuestionMove.FILE, target)
      for state in (ItemState.NEW, ItemState.WAITING_FOR_NATE, ItemState.WORKING, ItemState.ANSWERED)
      for target in (ItemState.WORKING, ItemState.ANSWERED, ItemState.CLOSED)),
})

_QUESTION_CLOSE_REFUSAL = "Only the asker can close this question after nate answers."


def _require_question_move(item: ListItem, move: QuestionMove, state: ItemState) -> None:
    if (isinstance(kind := item.question_kind, QuestionItemKind) and state is not item.state
            and (kind, item.state, move, state) not in _QUESTION_TRANSITIONS):
        raise ListError(_QUESTION_CLOSE_REFUSAL if move is QuestionMove.ASKER_CLOSE
                        else "Use the question actions to change its state.")


def _move_question(item: ListItem, move: QuestionMove, state: ItemState, moment: datetime,
                   source: str = PAGE_SOURCE, *, closing_words: str = "Question closed.") -> ListItem:
    _require_question_move(item, move, state)
    if state is item.state:
        return item
    if state is ItemState.CLOSED:
        event = FieldChange("Closed", {"closing_words": closing_words})
        item = replace(item, state=state, closed=moment, closing_words=closing_words,
                       unread_since=NoTimestamp.UNSET, parked_until=NoTimestamp.UNSET)
    else:
        event = FieldChange("Updated", {"state": state})
        unread = (moment if state in (ItemState.WAITING_FOR_NATE, ItemState.ANSWERED)
                  and not isinstance(item.unread_since, datetime) else item.unread_since)
        if state is ItemState.WORKING:
            unread = NoTimestamp.UNSET
        item = replace(item, state=state, closed=NoTimestamp.UNSET, closing_words="", unread_since=unread)
    return replace(item, updated=max(moment, item.updated), log=(*item.log, ItemChange(moment, event, source)))


def _unread_exchange(exchange: list_exchange.Exchange) -> bool:
    return isinstance(exchange.answer, list_exchange.ExchangeAnswer) and exchange.answer.read is list_exchange.AnswerRead.UNREAD


def _native_prompt(exchanges: Sequence[list_exchange.Exchange]) -> list_exchange.Exchange:
    return next((exchange for exchange in exchanges if exchange.direction is list_exchange.Direction.QUESTION_TO_NATE),
                exchanges[0])


_NO_ACKNOWLEDGED_EXCHANGES: frozenset[ReturnedAnswerId] = frozenset()


def question_filing_state(exchanges: Sequence[list_exchange.Exchange], filing: QuestionFiling, *,
                          acknowledged: frozenset[ReturnedAnswerId] = _NO_ACKNOWLEDGED_EXCHANGES) -> ItemState:
    """Use the same native question state for a save and a preview."""
    prompt = _native_prompt(exchanges)
    historical = filing is QuestionFiling.HISTORY
    if native_question_kind(prompt) is QuestionItemKind.TO_NATE:
        if prompt.answer is list_exchange.QuestionWithoutAnswer.OPEN:
            return ItemState.WAITING_FOR_NATE
        return (ItemState.CLOSED if historical or prompt.answer is list_exchange.QuestionWithoutAnswer.CLOSED
                else ItemState.WORKING)
    if any(_unread_exchange(exchange) and ReturnedAnswerId(exchange.source_key, exchange.exchange_id) not in acknowledged
           for exchange in exchanges):
        return ItemState.ANSWERED
    pending = any(exchange.answer is list_exchange.QuestionWithoutAnswer.OPEN for exchange in exchanges)
    return ItemState.WORKING if pending and not historical else ItemState.CLOSED


def _acknowledged_exchanges(item: ListItem) -> frozenset[ReturnedAnswerId]:
    unread = unread_answer_ids(item)
    return frozenset(entry.what.identity for entry in item.log
                     if isinstance(entry.what, ReturnedAnswer) and entry.what.identity not in unread)


def _file_question_kind(item: ListItem, before: ListItem, filing: QuestionFiling) -> ListItem:
    exchanges = question_item_exchanges(item)
    if not exchanges:
        return item
    kind = native_question_kind(_native_prompt(exchanges))
    assert isinstance(kind, QuestionItemKind)
    item = replace(item, question_kind=kind)
    if item.state is ItemState.CLOSED:
        return item
    before_exchanges = question_item_exchanges(before)
    historical = filing is QuestionFiling.HISTORY or bool(
        before_exchanges and isinstance(_native_prompt(before_exchanges).answer, list_exchange.ExchangeAnswer))
    mode = QuestionFiling.HISTORY if historical else filing
    state = question_filing_state(exchanges, mode, acknowledged=_acknowledged_exchanges(before))
    words = ("Closed when questions became items" if historical and kind is QuestionItemKind.TO_NATE
             else "Question closed.")
    return _move_question(item, QuestionMove.FILE, state, now(), closing_words=words)


def project_question_migration(item: ListItem) -> ListItem:
    """Project a legacy item without saving it."""
    if isinstance(item.question_kind, QuestionItemKind):
        return item
    return _file_question_kind(item, item, QuestionFiling.HISTORY)


def _with_filed_exchanges(item: ListItem, exchanges: tuple[list_exchange.FiledExchange, ...],
                          filing: QuestionFiling) -> ListItem:
    saved = {(exchange.source_key, exchange.exchange_id): exchange for exchange in item.filed_exchanges}
    returns: list[ItemChange] = []
    for exchange in exchanges:
        key = (exchange.source_key, exchange.exchange_id)
        previous = saved.get(key)
        current = previous.updated(exchange) if previous is not None else exchange
        saved[key] = current
        answer = current.answer
        if (current.source in (list_exchange.Source.SCOPED_REPLY, list_exchange.Source.STAFF_QUESTION)
                and current.direction is list_exchange.Direction.ANSWER_TO_NATE
                and isinstance(answer, list_exchange.ExchangeAnswer) and answer.read is list_exchange.AnswerRead.UNREAD
                and isinstance(answer.time, datetime)
                and (previous is None or isinstance(previous.answer, list_exchange.QuestionWithoutAnswer))):
            author = current.answerer
            source = author.name if isinstance(author, list_exchange.RecordedAnswerer) else author
            returns.append(ItemChange(answer.time, ReturnedAnswer(ReturnedAnswerId(*key), answer.text), source))
    changed = replace(item, filed_exchanges=tuple(saved.values()))
    if returns:
        returns.sort(key=lambda entry: entry.time)
        latest = returns[-1].time
        unread_since = max(item.unread_since, latest) if isinstance(item.unread_since, datetime) else latest
        changed = replace(changed, log=(*item.log, *returns), updated=max(item.updated, latest), unread_since=unread_since)
    if item.question_kind is NoQuestionItem.ORDINARY:
        return _file_question_kind(changed, item, filing)
    native = question_item_exchanges(changed)
    if not native or native[0].source_key != exchanges[0].source_key:
        return changed
    if item.state is ItemState.WAITING_FOR_NATE:
        answer = _native_prompt(native).answer
        if isinstance(answer, list_exchange.ExchangeAnswer):
            return _move_question(changed, QuestionMove.NATE_ANSWER, ItemState.WORKING, now())
        if answer is list_exchange.QuestionWithoutAnswer.CLOSED:
            return _move_question(changed, QuestionMove.WITHDRAW, ItemState.CLOSED, now())
    if returns and (item.state is ItemState.WORKING or
                    item.state is ItemState.CLOSED and item.question_kind is QuestionItemKind.FROM_NATE):
        return _move_question(changed, QuestionMove.REPLY, ItemState.ANSWERED, now())
    return changed


@dataclass(frozen=True)
class ExchangeDestination:
    destination: investigation.ListDestination
    reserved_new_item: bool


def _exchange_reservation(path: Path) -> ExchangeDestination | NextNumber:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return NextNumber.AVAILABLE
    mode, separator, location = value.partition(" ")
    keeper, list_prefix, number = location.partition("/L")
    if not separator or mode not in ("new", "linked") or not list_prefix or not re.fullmatch(r"[1-9][0-9]*", number):
        raise ListError(f"{path}: invalid exchange reservation")
    return ExchangeDestination(investigation.ListDestination(staff_id(keeper, "List keeper"), int(number)), mode == "new")


def _save_exchange_reservation(path: Path, destination: investigation.ListDestination, mode: Literal["new", "linked"]) -> None:
    write_text_atomic(path, f"{mode} {destination.keeper}/L{destination.number}\n", new_file_mode=0o600)


def _exchange_path(key: str) -> Path:
    return record_store.state_root() / ".list-exchanges" / hashlib.sha256(key.encode()).hexdigest()


@dataclass(frozen=True)
class LockedExchangeSource:
    key: str
    reservation: Path


@contextmanager
def lock_exchange_source(key: str) -> Generator[LockedExchangeSource]:
    reservation = _exchange_path(key)
    reservation.parent.mkdir(parents=True, exist_ok=True)
    with exclusive(reservation.with_suffix(".lock")):
        yield LockedExchangeSource(key, reservation)


def _read_pending_exchanges(reservation: Path) -> tuple[list_exchange.FiledExchange, ...]:
    try:
        return list_exchange.decode(reservation.with_suffix(".pending").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ()


def pending_exchanges(key: str, *, sources: list[Path] | None = None) -> tuple[list_exchange.FiledExchange, ...]:
    reservation = _exchange_path(key)
    if sources is not None:
        sources.append(reservation.with_suffix(".pending"))
    return _read_pending_exchanges(reservation)


def exchange_history(key: str) -> tuple[list_exchange.FiledExchange, ...]:
    """Read the destination row and any changes that still need a receipt."""
    reservation = _exchange_path(key)
    destination = _exchange_reservation(reservation)
    saved: tuple[list_exchange.FiledExchange, ...] = ()
    if isinstance(destination, ExchangeDestination):
        keeper, identity = destination.destination.keeper, f"L{destination.destination.number}"
        path = _store(keeper).path(identity)
        try:
            row = load(keeper, identity)
        except ListError:
            if path.exists():
                raise
        else:
            saved = tuple(exchange for exchange in row.filed_exchanges if exchange.source_key == key)
    return combine_exchange_history(saved, _read_pending_exchanges(reservation))


def combine_exchange_history(saved: tuple[list_exchange.FiledExchange, ...],
                             pending: tuple[list_exchange.FiledExchange, ...]) -> tuple[list_exchange.FiledExchange, ...]:
    history = {exchange.exchange_id: exchange for exchange in saved}
    for exchange in pending:
        previous = history.get(exchange.exchange_id)
        history[exchange.exchange_id] = previous.updated(exchange) if previous is not None else exchange
    return tuple(history.values())


def _exchange_receipt_line(destination: investigation.ListDestination, exchange: list_exchange.FiledExchange) -> str:
    digest = hashlib.sha256(list_exchange.encode((exchange,)).encode()).hexdigest()
    return f"{destination.keeper}/L{destination.number} {digest}"


def confirmed_exchanges(key: str, destination: investigation.ListDestination,
                        exchanges: Iterable[list_exchange.FiledExchange],
                        *, sources: list[Path] | None = None) -> frozenset[list_exchange.FiledExchange]:
    path = _exchange_path(key).with_suffix(".saved")
    if sources is not None:
        sources.append(path)
    try:
        receipt = set(path.read_text(encoding="utf-8").splitlines())
    except FileNotFoundError:
        return frozenset()
    return frozenset(exchange for exchange in exchanges if _exchange_receipt_line(destination, exchange) in receipt)


def _recover_exchange_allocation(store: record_store.NumberedRecords, journal: Path) -> None:
    try:
        value = journal.read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    saved = re.fullmatch(r"([a-f0-9]{64}) ([^ /\n]+)/L([1-9][0-9]*)\n", value)
    if saved is None:
        raise ListError(f"{journal}: invalid exchange allocation")
    digest, keeper, number_text = saved.groups()
    destination = investigation.ListDestination(staff_id(keeper, "List keeper"), int(number_text))
    reservation = journal.parent / ".list-exchanges" / digest
    previous = _exchange_reservation(reservation)
    if isinstance(previous, ExchangeDestination) and previous != ExchangeDestination(destination, True):
        raise ListError(f"{journal}: the exchange already has a different reservation")
    sync_folder(journal.parent)
    if store.next_number() <= destination.number:
        store.reserve_number(destination.number)
    else:
        sync_folder(store.folder())
    _save_exchange_reservation(reservation, destination, "new")
    journal.unlink()
    sync_folder(journal.parent)


@contextmanager
def _list_allocation(store: record_store.NumberedRecords) -> Generator[Path]:
    """Complete an interrupted exchange reservation before allocating another number."""
    with store.list_allocation():
        journal = record_store.state_root() / ".list-exchange-allocation"
        _recover_exchange_allocation(store, journal)
        yield journal


def _reserve_exchange_number(store: record_store.NumberedRecords, reservation: Path) -> ExchangeDestination:
    with _list_allocation(store) as journal:
        current = _exchange_reservation(reservation)
        if isinstance(current, ExchangeDestination):
            return current
        destination = investigation.ListDestination(store.owner, store.next_number())
        write_text_atomic(journal, f"{reservation.name} {destination.keeper}/L{destination.number}\n",
                          new_file_mode=0o600)
        _recover_exchange_allocation(store, journal)
        return ExchangeDestination(destination, True)


_NO_UNRESOLVED_REPLIES: frozenset[str] = frozenset()


def file_exchanges(source: LockedExchangeSource, exchanges: tuple[list_exchange.Exchange, ...], title: str, *,
                   linked: investigation.ListDestination | NextNumber,
                   owner: staff.StaffId, members: staff.StaffSnapshot,
                   question_filing: QuestionFiling = QuestionFiling.LIVE,
                   unresolved_reply_ids: frozenset[str] = _NO_UNRESOLVED_REPLIES) -> investigation.ListDestination:
    """Save one source and publish its receipt after the List row is durable."""
    key, reservation = source.key, source.reservation
    if not exchanges or any(exchange.source_key != key for exchange in exchanges):
        raise ListError("File one exchange source at a time.")
    filed = tuple(list_exchange.FiledExchange.from_exchange(exchange) for exchange in exchanges)
    reserved = _exchange_reservation(reservation)
    if isinstance(linked, investigation.ListDestination) and isinstance(reserved, NextNumber):
        if (record_store.state_root() / ".list-exchange-allocation").exists():
            keeper = record_store.list_keeper(conf.read_or_repository("console"), members)
            if keeper is None:
                raise ListError("The List keeper is unavailable.")
            with _list_allocation(_store(keeper.id)):
                reserved = _exchange_reservation(reservation)
    if isinstance(linked, investigation.ListDestination):
        if isinstance(reserved, ExchangeDestination):
            if reserved.destination != linked:
                raise ListError("The exchange source has two List destinations.")
        else:
            _save_exchange_reservation(reservation, linked, "linked")
            reserved = ExchangeDestination(linked, False)
    if isinstance(reserved, NextNumber):
        proposed = combine_exchange_history(_read_pending_exchanges(reservation), filed)
        conflicts = list_exchange.conflicting_reply_ids(proposed, members) | unresolved_reply_ids
        if all(exchange.exchange_id in conflicts for exchange in proposed):
            raise ConflictingExchangeReplies("The exchanges claim the same reply.")
        route = report_route(owner, members)
        reserved = _reserve_exchange_number(_store(route.keeper), reservation)
    destination = reserved.destination
    store = _store(destination.keeper)
    with store.locked():
        identity = f"L{destination.number}"
        new_row = reserved.reserved_new_item and not store.path(identity).exists()
        item = None if new_row else load(destination.keeper, identity)
        if item is None:
            history: tuple[list_exchange.FiledExchange, ...] = ()
        else:
            history = tuple(exchange for exchange in item.filed_exchanges if exchange.source_key == key)
            if reserved.reserved_new_item and not history:
                raise ListError(f"{identity} holds a different source.")
        pending_path = reservation.with_suffix(".pending")
        pending = _read_pending_exchanges(reservation)
        candidate = combine_exchange_history(combine_exchange_history(history, pending), filed)
        conflicts = list_exchange.conflicting_reply_ids(candidate, members) | unresolved_reply_ids
        candidate = tuple(exchange for exchange in candidate if exchange.exchange_id not in conflicts)
        if not candidate:
            raise ConflictingExchangeReplies("The exchanges claim the same reply.")
        disputed_pending = tuple(exchange for exchange in pending if exchange.exchange_id in conflicts)
        staged = (*candidate, *disputed_pending)
        safe_history = tuple(exchange for exchange in history if exchange.exchange_id not in conflicts)
        if candidate != safe_history and staged != pending:
            write_text_atomic(pending_path, list_exchange.encode(staged), new_file_mode=0o600)
            pending = staged
        if item is None:
            item = _save_new_exchanges(store, destination.number, title, owner, report_route(owner, members),
                                       candidate, question_filing)
        else:
            item = _save_linked_exchanges(store, item, candidate, question_filing)
        saved = tuple(exchange for exchange in item.filed_exchanges if exchange.source_key == key)
        confirmed = confirmed_exchanges(key, destination, saved)
        receipted = tuple(exchange for exchange in saved if exchange.exchange_id not in conflicts or exchange in confirmed)
        if frozenset(receipted) != confirmed:
            receipt = "".join(_exchange_receipt_line(destination, exchange) + "\n" for exchange in receipted)
            write_text_atomic(reservation.with_suffix(".saved"), receipt, new_file_mode=0o600)
        if disputed_pending:
            if disputed_pending != pending:
                write_text_atomic(pending_path, list_exchange.encode(disputed_pending), new_file_mode=0o600)
        else:
            pending_path.unlink(missing_ok=True)
    return destination


def _save_linked_exchanges(store: record_store.NumberedRecords, item: ListItem,
                          exchanges: tuple[list_exchange.FiledExchange, ...], filing: QuestionFiling) -> ListItem:
    changed = _with_filed_exchanges(item, exchanges, filing)
    if changed != item:
        with record_store.keep_file_on_error(store.path(item.id)):
            _save(store, changed)
    else:
        sync_folder(store.folder())
    return changed


def _save_new_exchanges(store: record_store.NumberedRecords, number: int, title: str, owner: staff.StaffId,
                        route: ReportRoute, exchanges: tuple[list_exchange.FiledExchange, ...], filing: QuestionFiling) -> ListItem:
    item = _new_item(number, title, owner, store.owner, PAGE_SOURCE, {"producer": route.producer, "unit": route.unit})
    _require_active_open_producers((item,))
    item = _with_filed_exchanges(replace(item, unread_since=NoTimestamp.UNSET), exchanges, filing)
    with record_store.keep_file_on_error(store.path(item.id)):
        _save(store, item)
    return item


def migrate_question_item(keeper: staff.StaffId, identity: str) -> None:
    store = _store(keeper)
    with store.locked():
        item = load(keeper, identity)
        changed = project_question_migration(item)
        if changed != item:
            with record_store.keep_file_on_error(store.path(identity)):
                _save(store, changed)


def investigation_report_kind(record: investigation.Investigation) -> ReportKind | NoListReport:
    match record.purpose:
        case investigation.InvestigationPurpose.BUG_REPORT | investigation.InvestigationPurpose.MOVING_LEGACY_BUG:
            return ReportKind.BUG
        case investigation.InvestigationPurpose.ENHANCEMENT:
            return ReportKind.ENHANCEMENT
        case investigation.InvestigationPurpose.LIST_INVESTIGATION:
            return ReportKind.INVESTIGATION
        case investigation.InvestigationPurpose.INVESTIGATE:
            return NoListReport.ABSENT


def report_from_investigation(record: investigation.Investigation, kind: ReportKind, key: str) -> ListReport:
    words = record.question.removeprefix("Enhancement: ") if kind is ReportKind.ENHANCEMENT else record.question
    return ListReport(kind, key, words, record.submission_scope or NoReportScope.NATIVE_RECORD)


def file_investigation_report(
    record: investigation.Investigation, report_kind: ReportKind, members: staff.StaffSnapshot, *,
    route: review_submissions.ListFilingRoute | review_submissions.NoListFiling,
    reservation: Path | NextNumber, source: str,
) -> investigation.ListDestination:
    from scripts.review_links import review_items

    if isinstance(route, dict):
        keeper, owner, producer, unit = route['keeper'], route['owner'], route['producer'], route['unit']
        source = route['source']
    else:
        owner = record.owner
        report_recipients = report_route(owner, members)
        keeper, producer, unit = report_recipients.keeper, report_recipients.producer, report_recipients.unit
    report = report_from_investigation(record, report_kind, review_items.investigation_record_key(record))
    number = file_report(keeper, report, record.title, owner=owner, producer=producer,
                         unit=unit, source=source, reservation=reservation)
    return investigation.ListDestination(keeper, number)


@dataclass(frozen=True)
class ReportRoute:
    keeper: staff.StaffId
    producer: staff.StaffId
    unit: str


def report_route(owner: staff.StaffId, members: staff.StaffSnapshot) -> ReportRoute:
    """Find the keeper, producer and unit for a report without a saved route."""
    keeper = record_store.list_keeper(conf.read_or_repository("console"), members)
    if keeper is None:
        raise ListError("The List has no active producer.")
    member = members.by_id(owner)
    unit = member.name if member is not None and member.kind == "unit" else ""
    mapped = producer_map.producer_for(owner, members)
    producer = mapped.producer if isinstance(mapped, producer_map.MappedProducer) else keeper
    return ReportRoute(keeper.id, producer.id, unit)


@contextmanager
def file_cli_investigation(owner: staff.StaffId, title: str, words: str, reason: str,
                           members: staff.StaffSnapshot) -> Generator[str]:
    """File the List report and retain its native ID until the CLI prints it."""
    _ = report_route(owner, members)
    with investigation.cli_filing(owner, title, words, reason) as (path, report):
        finish_cli_investigation(path, members)
        yield report.id


def finish_cli_investigation(path: Path, members: staff.StaffSnapshot) -> None:
    from scripts.review_links import review_submissions

    _ = investigation.file_on_list(path, lambda current: file_investigation_report(
        current, ReportKind.INVESTIGATION, members, route=review_submissions.NoListFiling(),
        reservation=NextNumber.AVAILABLE, source="nate via investigation file"))


def move_review_bugs(members: staff.StaffSnapshot) -> list[investigation.ListDestination]:
    """Move every open Review Bug once and retain its native evidence."""
    from scripts.review_links import review_submissions

    moved: list[investigation.ListDestination] = []
    owners = record_store.store_owners(record_store.RecordKind.INVESTIGATION, strict=True)
    for owner in sorted(owners):
        for path in investigation.review_record_paths(owner, strict=True):
            fields = frozenset(('kind', 'state', 'list_destination', 'purpose'))
            header = record_store.read_header(path, keys=fields, strict=True)
            if (header.get('kind') != [investigation.InvestigationKind.BUG]
                    or header.get('state') == [investigation.InvestigationState.READ]
                    or header.get('list_destination')
                    or header.get('purpose', []) not in ([], [investigation.InvestigationPurpose.MOVING_LEGACY_BUG])):
                continue

            def file_moved(current: investigation.Investigation) -> investigation.ListDestination:
                destination = file_investigation_report(
                    current, ReportKind.BUG, members, route=review_submissions.NoListFiling(),
                    reservation=NextNumber.AVAILABLE, source="moved from Review")
                moved.append(destination)
                return destination

            _ = investigation.file_on_list(path, file_moved, mode=investigation.ListFilingMode.MOVE_BUG)
    return moved


def move_review_waits(root: Path, members: staff.StaffSnapshot) -> list[investigation.ListDestination]:
    """File open Review waits and leave a destination in each source log."""
    from scripts.review_links import review_items

    move_wait = cast(Callable[[Path, review_items.ReviewItem, staff.StaffSnapshot],
                             investigation.ListDestination | investigation.NoListDestination],
                     import_module('scripts.review_links.review_actions').move_wait_to_list)
    moved: list[investigation.ListDestination] = []
    for item in review_items.read(root, members=members):
        if review_items.can_move_wait(item):
            destination = move_wait(root, item, members)
            if isinstance(destination, investigation.ListDestination):
                moved.append(destination)
    return moved


@dataclass(frozen=True)
class ReservedReportNumber:
    number: int


def reserve_moved_report(keeper: staff.StaffId, evidence_key: str) -> ReservedReportNumber:
    """Select the number for a source marker before the source writer runs."""
    store = _store(keeper)
    with _list_allocation(store):
        items = records(keeper, include_closed=True)
        for item in items:
            if isinstance(item.report, ListReport) and item.report.evidence_key == evidence_key:
                return ReservedReportNumber(item.number)
        for item in items:
            if (isinstance(item.report, NoListReport) and item.state != ItemState.CLOSED
                    and evidence_key in item.reviews):
                return ReservedReportNumber(item.number)
        number = store.next_number()
        store.reserve_number(number)
        return ReservedReportNumber(number)


def file_report(keeper: staff.StaffId, report: ListReport, title: str, *, owner: ItemOwner,
                producer: staff.StaffId, unit: str, source: str,
                reservation: Path | NextNumber | ReservedReportNumber = NextNumber.AVAILABLE,
                state: ItemState = ItemState.NEW,
                retry: ReportRetry = ReportRetry.KEEP) -> int:
    """File a report once, or add its evidence to an existing linked List item."""
    store = _store(keeper)
    with _list_allocation(store):
        reserved = reserved_review_number(reservation) if isinstance(reservation, Path) else reservation
        if isinstance(reserved, ReservedReportNumber):
            reserved = reserved.number
            candidates = records(keeper, include_closed=True)
        elif isinstance(reserved, int) and store.path(f"L{reserved}").is_file():
            candidates = (load(keeper, f"L{reserved}"),)
        else:
            candidates = records(keeper, include_closed=True)
        linked = next((item for item in candidates if isinstance(item.report, NoListReport)
                       and item.state != ItemState.CLOSED and report.evidence_key in item.reviews), None)
        for item in candidates:
            if isinstance(item.report, ListReport) and (
                    item.report.evidence_key == report.evidence_key
                    or report.submission_scope and item.report.submission_scope == report.submission_scope):
                if isinstance(reservation, Path) and isinstance(reserved, NextNumber):
                    write_text_atomic(reservation, f"{item.number}\n", new_file_mode=0o600)
                if (retry is ReportRetry.UPDATE and item.report.kind is report.kind
                        and item.report.evidence_key == report.evidence_key):
                    updated_report = replace(item.report, words=report.words, validation_log=report.validation_log)
                    if updated_report != item.report:
                        _save(store, replace(item, report=updated_report, updated=now()))
                sync_folder(store.folder())
                return item.number
        if linked is not None:
            reviews = (linked.reviews if not report.evidence_key.startswith('investigation/')
                       else tuple(key for key in linked.reviews if key != report.evidence_key))
            changes: ItemChanges = {}
            if report.kind is ReportKind.VALIDATION and linked.owner != owner:
                changes['owner'] = owner
            if linked.state not in (ItemState.NEW, ItemState.WORKING):
                changes['state'] = ItemState.WORKING
            item = replace(linked, report=report, reviews=reviews, **changes)
            if changes:
                moment = now()
                change = ItemChange(moment, FieldChange('Updated', {**changes}), source)
                item = replace(item, updated=moment, log=(*linked.log, change))
        elif isinstance(reserved, int) and any(item.number == reserved for item in candidates):
            raise ListError(f"L{reserved}: the report reservation names a different item")
        else:
            chosen = store.next_number() if isinstance(reserved, NextNumber) else reserved
            reviews = () if report.evidence_key.startswith('investigation/') else (report.evidence_key,)
            item = replace(_new_item(chosen, title, owner, keeper, source,
                                     {"producer": producer, "unit": unit, "state": state}, reviews=reviews),
                           report=report)
            _require_active_open_producers((item,))
            if isinstance(reserved, NextNumber):
                store.reserve_number(chosen)
        if isinstance(reservation, Path) and isinstance(reserved, NextNumber):
            write_text_atomic(reservation, f"{item.number}\n", new_file_mode=0o600)
        _save(store, item)
        return item.number


def _change(keeper: staff.StaffId, identity: str, edit: Callable[[ListItem, datetime], ListItem],
            what: ListEvent | Callable[[ListItem], ListEvent], source: str, *,
            unread: bool | Callable[[ListItem], bool] = True,
            page_action: PageItemNotice | PageRequest | NoPageRequest = NoPageRequest.ABSENT,
            assigned_to: staff.StaffId | AllItems = AllItems.INCLUDED,
            question_move: QuestionMove = QuestionMove.OTHER,
            follow: Callable[[ListItem, ListItem, datetime], tuple[ItemChange, ...]] | None = None) -> ListEdit:
    """Save one edit and its log entry under the write lock.

    `follow` gives the entries that come after the edit's own entry, from the item before and after the edit.
    """
    store = _store(keeper)
    _ = store.path(identity)
    with store.locked():
        if assigned_to is AllItems.INCLUDED:
            item = load(keeper, identity)
        else:
            fields, body = store.read(identity)
            if staff_id(fields.get("producer", keeper), "producer") != assigned_to:
                raise ListError(f"{identity} is assigned to another producer. "
                                + "A producer can change only its own items.")
            item = _read_item(store, identity, fields, body)
        request = page_action
        if isinstance(page_action, PageItemNotice):
            if not page_action.words.strip():
                raise ListError("an item notice needs words")
            request = PageRequest(page_action.request, page_action.action, page_action.words, page_action.form_words,
                                  page_action.parked_until)
        if isinstance(request, PageRequest) and _previous_page_request(item, request) is not NoPageRequest.ABSENT:
            return UnchangedEdit(item)
        moment = now()
        changed = edit(item, moment)
        _require_question_move(item, question_move, changed.state)
        if changed.state is not item.state:
            changed = _settle_run_confirmations(changed, moment)
        entries: list[ItemChange] = []
        schedule_changed = _schedule_fields(item.schedule) != _schedule_fields(changed.schedule)
        if changed != item or schedule_changed:
            event = what(changed) if callable(what) else what
            if isinstance(event, FieldChange) and schedule_changed:
                log_fields: LogFields = {**event.fields, "schedule": changed.schedule}
                if event.action != "Closed" and item.state != changed.state:
                    log_fields["state"] = changed.state
                if event.action != "Closed" and item.parked_until != changed.parked_until:
                    log_fields["parked_until"] = changed.parked_until
                event = replace(event, fields=log_fields)
            entries.append(ItemChange(moment, event, source))
            if follow is not None:
                entries.extend(follow(item, changed, moment))
            stays_unread = unread(changed) if callable(unread) else unread
            changed = replace(changed, updated=moment, unread_since=moment if stays_unread else NoTimestamp.UNSET)
            if (isinstance(event, FieldChange) and event.action == "Updated"
                    and event.fields.get("state") == ItemState.DELIVERED
                    and changed.parked_until is ParkUntilDelivered.DELIVERED):
                changed = replace(changed, parked_until=NoTimestamp.UNSET)
                entries.append(ItemChange(moment, UnparkedForDelivery(ItemDelivered.RECORDED), source))
        if isinstance(request, PageRequest):
            if entries and isinstance(page_action, PageItemNotice):
                pending = PageFeedback(page_action.request, page_action.words, action=page_action.action,
                                       form_words=request.form_words, parked_until=request.parked_until,
                                       recipient=page_action.recipient, origin=page_action.origin)
                if page_action.action == ReportVerdict.NOT_FIXED:
                    assert not isinstance(item.owner, UnassignedOwner)
                    pending = replace(pending, recipient=item.producer, additional_recipient=item.owner)
                entries.append(ItemChange(moment, pending, PAGE_SOURCE))
            elif request.action == "read" and item.question_kind is not NoQuestionItem.ORDINARY:
                if request.words and not isinstance(item.owner, UnassignedOwner):
                    notice = PageFeedback(request.request, request.words, action="read", form_words=request.words,
                                          recipient=item.owner,
                                          origin=PageNoticeOrigin.STUDIO)
                    entries.append(ItemChange(moment, notice, PAGE_SOURCE))
                else:
                    entries.insert(0, ItemChange(moment, request, PAGE_SOURCE))
            elif request.action == "read" and changed.state != item.state:
                notice = _next_step_notice(keeper, request.request, "nate read", changed, form_words=request.words)
                entries.append(ItemChange(moment, notice, PAGE_SOURCE))
            elif request.action == "unpark":
                reason = (UnparkNoChangeReason.EXPIRED if isinstance(item.parked_until, datetime)
                          else UnparkNoChangeReason.NO_ACTIVE_PARKING)
                entries.insert(0, ItemChange(moment, UnparkNoChangeReceipt(request, reason), PAGE_SOURCE))
            else:
                entries.insert(0, ItemChange(moment, request, PAGE_SOURCE))
        if not entries:
            return UnchangedEdit(item)
        changed = replace(changed, log=(*changed.log, *entries))
        _validate(changed)
        if item.state == ItemState.CLOSED or changed.producer != item.producer:
            _require_active_open_producers((changed,))
        if isinstance(request, PageRequest):
            changed = _save_page_request(store, keeper, changed, request)
        else:
            _save(store, changed, previous_producer=item.producer)
        return SavedEdit(item, changed)


def _open(item: ListItem) -> None:
    if item.state == ItemState.CLOSED:
        raise ListError(f"{item.id}: reopen the Closed item before changing it")


class RepairCheckRefused(ListError):
    """The List rejected this request before saving its verdict."""

    def __init__(self, request: str, reason: str) -> None:
        super().__init__(reason)
        self.request: str = request


# Use the same edge whitespace in Python and the List form.
REPORT_WORD_WHITESPACE = ("\t\n\v\f\r\x1c\x1d\x1e\x1f \x85\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006"
                          "\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000")


def check_report(keeper: staff.StaffId, identity: str, verdict: ReportVerdict, words: str, *,
                 request: str, revision: RepairRevision | NoRepairRevision) -> ListItem:
    """Save nate's verdict and the pending Not fixed message in one List write."""
    if verdict is ReportVerdict.NOT_FIXED:
        words = words.strip(REPORT_WORD_WHITESPACE)
        if not words or len(words) > 10000:
            raise RepairCheckRefused(request, "Enter your words for the owner and the producer.")
    state = ItemState.DELIVERED if verdict is ReportVerdict.WORKS else ItemState.WORKING

    def check(item: ListItem, _: datetime) -> ListItem:
        if not isinstance(revision, RepairRevision) or revision != item.repair_revision:
            raise RepairCheckRefused(request, "The repair changed. Open the item and check it again.")
        if verdict is ReportVerdict.NOT_FIXED and isinstance(item.owner, UnassignedOwner):
            raise RepairCheckRefused(request, "Assign an owner before sending Not fixed.")
        return replace(item, state=state, next_step="")

    action = (PageItemNotice(request, words, ReportVerdict.NOT_FIXED, origin=PageNoticeOrigin.STUDIO)
              if verdict is ReportVerdict.NOT_FIXED else PageRequest(request, ReportVerdict.WORKS, ""))
    result = _change(keeper, identity, check, FieldChange("Updated", {"state": state, "next_step": ""}), PAGE_SOURCE,
                     unread=False, page_action=action)
    return result.item


def make_top_priority(keeper: staff.StaffId, identity: str, *,
                      assigned_to: staff.StaffId | AllItems = AllItems.INCLUDED) -> ListItem:
    """Make an open item the top priority item of its producer. A producer names only its own items."""
    item = load(keeper, identity)
    if item.state == ItemState.CLOSED:
        raise ListError(f"{item.id} is Closed. Only an open item can be the top priority item.")
    if assigned_to is not AllItems.INCLUDED and item.producer != assigned_to:
        raise ListError(f"{item.id} is assigned to another producer")
    try:
        _ = staff.set_producer_top_item(item.producer, staff.TopItem(item.number))
    except (staff.StaffRefused, staff.StaffNotFound) as error:
        raise ListError(f"{item.id}: {error}") from error
    return item


def _without_evidence_block(evidence: str, start: int, end: int) -> str:
    before = re.sub(r"(?:\r?\n[ \t]*)+\Z", "", evidence[:start])
    if not before.strip():
        before = ""
    after = re.sub(r"\A(?:[ \t]*\r?\n)+", "", evidence[end:])
    return before + (("\n\n" if before else "") + after if after.strip() else "")


def _without_checkins_copy(evidence: str) -> str:
    marker = re.search(r"(?m)^Source checkins\.json item:\r?\n", evidence)
    if marker is None:
        return evidence
    tail = evidence[marker.end():]
    copied_text = tail.lstrip()
    try:
        copied, end = cast(tuple[object, int], json.JSONDecoder().raw_decode(copied_text))
    except ValueError:
        return evidence
    if not isinstance(copied, dict) or re.match(r"[ \t]*(?:\r?\n|\Z)", copied_text[end:]) is None:
        return evidence
    return _without_evidence_block(evidence, marker.start(), marker.end() + len(tail) - len(copied_text) + end)


def _without_row_copy(evidence: str, number: int) -> str:
    heading = (rf"(?m)^## {number}\. [^\r\n]+\r?\n(?:[ \t]*\r?\n)+"
               + r"\*\*List state:\*\*[^\r\n]*\r?\n(?:[ \t]*\r?\n)+(?=- \*\*[^*\r\n]+:\*\*)")
    for match in reversed(list(re.finditer(heading, evidence))):
        end = match.end()
        for line in evidence[end:].splitlines(keepends=True):
            if line.strip() and re.match(r"(?:- \*\*[^*\r\n]+:\*\*|(?: {2,}|\t)[ \t]*\S)", line) is None:
                break
            end += len(line)
        evidence = _without_evidence_block(evidence, match.start(), end)
    return evidence


def clean_evidence(keeper: staff.StaffId, *, source: str) -> tuple[int, int]:
    """Remove migration copies from each record's Evidence section."""
    _log_line(source, "source")
    store = _store(keeper)
    changed = unchanged = 0
    with store.locked():
        for item in records(keeper, include_closed=True):
            evidence = _without_checkins_copy(item.evidence)
            cleaned = _without_row_copy(evidence, item.number)
            if cleaned == item.evidence:
                unchanged += 1
                continue
            moment = now()
            entries: list[ItemChange] = []
            if evidence != item.evidence:
                entries.append(ItemChange(moment, EvidenceCopyRemoved("checkins.json"), source))
            if cleaned != evidence:
                entries.append(ItemChange(moment, EvidenceCopyRemoved("the List row"), source))
            _save(store, replace(item, evidence=cleaned, updated=moment, log=(*item.log, *entries)))
            changed += 1
    return changed, unchanged


def schedule_revision(schedule: Schedule) -> str:
    return hashlib.sha256(repr(schedule).encode()).hexdigest()


def _require_schedule_selection(
        schedule: Schedule, scope: ScheduleScope,
        expected: ScheduleExpectation | UseCurrentSchedule = UseCurrentSchedule.AT_WRITE, *,
        uses_selected_start: bool = False) -> None:
    if isinstance(expected, ScheduleExpectation):
        if expected.revision and expected.revision != schedule_revision(schedule):
            if isinstance(schedule, ScheduledSeries) and (uses_selected_start or scope in (ScheduleScope.THIS, ScheduleScope.FOLLOWING)):
                _require_expected_run(schedule.next_run(), expected)
            raise ScheduleSelectionChanged("The schedule changed. Open the item again.")
        if scope in (ScheduleScope.THIS, ScheduleScope.FOLLOWING) and not isinstance(schedule, ScheduledSeries):
            raise ScheduleSelectionChanged("The series changed. Open the item again.")
    if isinstance(schedule, ScheduledSeries):
        if scope is ScheduleScope.UNSPECIFIED:
            raise ListError("use --scope this, following, or all for a repeated item")
    elif scope is not ScheduleScope.UNSPECIFIED:
        raise ListError("--scope needs a repeated item")


def _require_expected_run(selected: SeriesRun | repeat_rule.NoNextRun,
                          expected: ScheduleExpectation | UseCurrentSchedule) -> None:
    if isinstance(expected, ScheduleExpectation):
        number = selected.number if isinstance(selected, SeriesRun) else selected
        if number != expected.run_number:
            raise ScheduleSelectionChanged("The next run changed. Open the item again.")


def _pending_series_run(series: ScheduledSeries,
                        expected: ScheduleExpectation | UseCurrentSchedule) -> SeriesRun:
    selected = series.next_run()
    _require_expected_run(selected, expected)
    if not isinstance(selected, SeriesRun):
        raise ListError("the series has no next run. Use --scope all")
    return selected


def _require_following_run(series: ScheduledSeries) -> None:
    if not series.finished_runs:
        raise ListError("--scope following is available from the second run. Use --scope all until a run occurs")


def _replace_series_run(series: ScheduledSeries, number: int, run: ScheduledRun | SkippedRun) -> ScheduledSeries:
    exceptions = tuple(entry for entry in series.exceptions if entry.number != number)
    return replace(series, exceptions=(*exceptions, RunException(number, run)))


def _edit_all_runs(series: ScheduledSeries, run: ScheduledRun,
                   rule: repeat_rule.RepeatRule, *, change_words: bool) -> ScheduledSeries:
    same_pattern = replace(rule, end=series.rule.end) == series.rule
    same_start = _schedule_time_text(run.start) == _schedule_time_text(series.first.start)
    exceptions = tuple(
        replace(entry, run=replace(entry.run, words=run.words))
        if change_words and isinstance(entry.run, ScheduledRun) and entry.number not in series.finished_runs
        else entry
        for entry in series.exceptions
        if isinstance(entry.run, SkippedRun) or (same_pattern and same_start)
    )
    following_runs = UseRuleEnd.RULE if rule != series.rule else series.following_runs
    return replace(series, first=run, rule=rule, exceptions=exceptions, following_runs=following_runs)


def _split_series(series: ScheduledSeries, selected: SeriesRun, pending: ScheduledRun, edits: ScheduleChanges,
                  rule: repeat_rule.RepeatRule, moment: datetime, *, compact_completed_prefix: bool) -> ScheduledSeries:
    instant = _validate_schedule_time(pending.start)
    if instant < moment.astimezone(UTC):
        raise ListError("choose a time on or after now for this and following runs")
    reanchors = (instant != selected.run.start.astimezone(UTC)
                 or pending.start.tzinfo != series.first.start.tzinfo
                 or replace(rule, end=series.rule.end) != series.rule)
    if not reanchors:
        first = replace(series.first, words=edits.get("words", series.first.words))
        changed = _edit_all_runs(series, first, rule, change_words="words" in edits)
        if "words" in edits:
            earlier = tuple(RunException(run.number, run.run) for run in series.runs()
                            if run.number < selected.number and run.run.words != first.words)
            earlier_numbers = {entry.number for entry in earlier}
            changed = replace(changed, exceptions=(
                *(entry for entry in changed.exceptions if entry.number not in earlier_numbers), *earlier))
        if (compact_completed_prefix and edits.keys() == {"words"} and changed != series
                and series.following_runs is UseRuleEnd.RULE
                and len(series.finished_runs) == selected.number - 1
                and max(series.finished_runs, default=0) < selected.number
                and _schedule_time_text(pending.start) == _schedule_time_text(selected.original)):
            # Keep the same cadence and allowance after a completed prefix.
            end = rule.end
            if isinstance(end, repeat_rule.RunCount):
                rule = replace(rule, end=repeat_rule.RunCount(series.pending_count(end)))
            return replace(changed, first=pending, rule=rule, run_number_offset=selected.number - 1,
                           exceptions=tuple(entry for entry in changed.exceptions if entry.number > selected.number))
        end = rule.end
        different_end = end != series.rule.end
        if "repeat" not in edits or (not different_end and isinstance(end, repeat_rule.RepeatUntilLimit)):
            return changed
        # COUNT in a following edit measures pending runs from the selected run.
        changes_count = isinstance(end, repeat_rule.RunCount) and end.count != series.pending_count(end)
        run_allowance = series_run_limit() if different_end or changes_count else series.run_allowance
        retained = series.select_following_runs(selected, rule, run_allowance)
        return replace(changed, run_allowance=run_allowance, following_runs=retained)

    first = ScheduledRun(pending.start, edits.get("words", series.first.words))
    settled = series.finished_runs | series.skipped_runs
    run_number_offset = max(selected.number - 1, max(settled, default=0))
    exceptions = tuple(entry for entry in series.exceptions if isinstance(entry.run, SkippedRun))
    if "repeat" not in edits and isinstance(rule.end, repeat_rule.RunCount):
        rule = replace(rule, end=repeat_rule.RunCount(series.pending_count(rule.end)))
    following = replace(series, first=first, rule=rule, run_number_offset=run_number_offset, exceptions=exceptions,
                        run_allowance=series_run_limit(), following_runs=UseRuleEnd.RULE)
    if _schedule_fields(pending) != _schedule_fields(first):
        following = _replace_series_run(following, run_number_offset + 1, pending)
    return following


def _schedule_edit(schedule: Schedule, edits: ScheduleChanges, scope: ScheduleScope, moment: datetime, *,
                   repeat_zone: ZoneInfo | RepeatInSelectedRunZone,
                   expected: ScheduleExpectation | UseCurrentSchedule = UseCurrentSchedule.AT_WRITE) -> Schedule:
    uses_selected_start = "repeat" in edits and "start" not in edits
    _require_schedule_selection(schedule, scope, expected, uses_selected_start=uses_selected_start)

    def edited_run(run: ScheduledRun, rule: repeat_rule.RepeatRule | RemoveRepeat) -> ScheduledRun:
        start = edits.get("start", run.start)
        if isinstance(rule, repeat_rule.RepeatRule):
            if "repeat" in edits:
                zone = repeat_zone if isinstance(repeat_zone, ZoneInfo) else run.start.tzinfo
            else:
                assert isinstance(schedule, ScheduledSeries)
                zone = schedule.first.start.tzinfo
            assert zone is not None
            start = start.astimezone(zone)
        return ScheduledRun(start, edits.get("words", run.words))

    if isinstance(schedule, NoSchedule):
        if "start" not in edits or "words" not in edits:
            raise ListError("use --start and --do for a new schedule")
        schedule = ScheduledRun(edits["start"], edits["words"])
    if isinstance(schedule, ScheduledRun):
        rule = edits.get("repeat", RemoveRepeat.KEEP_ONE_RUN)
        run = edited_run(schedule, rule)
        return ScheduledSeries(run, rule, series_run_limit()) if isinstance(rule, repeat_rule.RepeatRule) else run

    if scope is ScheduleScope.THIS:
        if "repeat" in edits:
            raise ListError("change a repeat rule with --scope following or all")
        selected = _pending_series_run(schedule, expected)
        run = edited_run(selected.run, RemoveRepeat.KEEP_ONE_RUN)
        if _schedule_fields(run) == _schedule_fields(selected.run):
            return schedule
        if _validate_schedule_time(run.start) < moment.astimezone(UTC):
            raise ListError("choose a time on or after now for this run")
        return _replace_series_run(schedule, selected.number, run)

    rule = edits.get("repeat", schedule.rule)
    if scope is ScheduleScope.FOLLOWING:
        selected = _pending_series_run(schedule, expected)
        _require_following_run(schedule)
        if isinstance(rule, repeat_rule.RepeatRule):
            return _split_series(schedule, selected, edited_run(selected.run, rule), edits, rule, moment,
                                 compact_completed_prefix=isinstance(expected, ScheduleExpectation))
        base = selected.run
    else:
        base = schedule.first
        if rule is RemoveRepeat.KEEP_ONE_RUN or (
                uses_selected_start and isinstance(expected, ScheduleExpectation)):
            selected = schedule.next_run()
            _require_expected_run(selected, expected)
            if isinstance(selected, SeriesRun):
                base = selected.run if rule is RemoveRepeat.KEEP_ONE_RUN else replace(base, start=selected.run.start)
            elif rule is RemoveRepeat.KEEP_ONE_RUN and "start" not in edits:
                raise ListError("the series has no next run. Use --scope all with a new start")

    run = edited_run(base, rule)
    if rule is RemoveRepeat.KEEP_ONE_RUN:
        if _validate_schedule_time(run.start) < moment.astimezone(UTC):
            raise ListError("choose a time on or after now for the one scheduled run")
        return run
    changed = _edit_all_runs(schedule, run, rule, change_words="words" in edits)
    if changed.rule != schedule.rule or _schedule_time_text(changed.first.start) != _schedule_time_text(schedule.first.start):
        return replace(changed, run_allowance=series_run_limit())
    return changed


def _remove_schedule(schedule: Schedule, scope: ScheduleScope,
                     expected: ScheduleExpectation | UseCurrentSchedule = UseCurrentSchedule.AT_WRITE, *,
                     words: str = "") -> Schedule:
    _require_schedule_selection(schedule, scope, expected)
    if not isinstance(schedule, ScheduledSeries) or scope is ScheduleScope.ALL:
        return NoSchedule.UNSET
    selected = _pending_series_run(schedule, expected)
    if scope is ScheduleScope.FOLLOWING:
        _require_following_run(schedule)
        return NoSchedule.UNSET
    series = _replace_series_run(schedule, selected.number, SkippedRun(words))
    series = replace(series, runs_used=series.runs_used + 1)
    return series if isinstance(series.next_run(), SeriesRun) else NoSchedule.UNSET


def _confirmation_wait() -> timedelta:
    settings = conf.read("the_list")
    wait = settings.origin("confirm_wait_minutes", conf.positive_whole)
    if not isinstance(wait, conf.UsableLiveValue):
        raise ListError(f"a scheduled run needs a valid confirm_wait_minutes in {settings.source}")
    try:
        return timedelta(minutes=wait.value)
    except OverflowError:
        raise ListError("confirm_wait_minutes is too large") from None


def _with_run_delivery(item: ListItem, delivery: RunDelivery, moment: datetime, *,
                       uncertain: UncertainNotice | DeliveryUncertainty = DeliveryUncertainty.NONE) -> ListItem:
    log = list(item.log)
    if delivery.log_index == len(log):
        log.append(ItemChange(moment, delivery.log_text(), "the List"))
    else:
        log[delivery.log_index] = replace(log[delivery.log_index], what=delivery.log_text())
    if isinstance(uncertain, UncertainNotice):
        due = _schedule_time_text(delivery.run.start)
        log.append(ItemChange(moment, f"Scheduled run due {due}. Delivery uncertain for {uncertain.kind}.", "the List"))
    if delivery.log_index == len(item.log):
        deliveries = (*item.run_deliveries, delivery)
    else:
        deliveries = tuple(delivery if other.key == delivery.key else other for other in item.run_deliveries)
    active = tuple(run for run in deliveries
                   if not isinstance(run.confirmation, RunResult) or any(notice.needs_check() for notice in run.notices))
    state = item.state
    if (not active and state is ItemState.WORKING and item.schedule is not NoSchedule.UNSET
            and delivery.confirmation is not RunResult.MANUALLY_SETTLED):
        state = ItemState.SCHEDULED
    changed = replace(item, log=tuple(log), run_deliveries=active, state=state)
    if changed.log != item.log or state != item.state:
        changed = replace(changed, updated=moment, unread_since=moment)
    return changed


def _settle_run_confirmations(item: ListItem, moment: datetime) -> ListItem:
    for delivery in item.run_deliveries:
        if isinstance(delivery.confirmation, AwaitingConfirmation):
            settled = replace(delivery, confirmation=RunResult.MANUALLY_SETTLED)
            item = _with_run_delivery(item, settled, moment)
    return item


def _due_schedule(schedule: Schedule, moment: datetime, normal_window: timedelta) -> DueRuns | NoDueRun:
    instant = moment.astimezone(UTC)
    if isinstance(schedule, ScheduledRun) and schedule.start.astimezone(UTC) <= instant:
        return DueRuns((schedule,), NoSchedule.UNSET)
    if not isinstance(schedule, ScheduledSeries):
        return NoDueRun.YET
    due: list[SeriesRun] = []
    for run in schedule.runs():
        if run.run.start.astimezone(UTC) > instant:
            break
        due.append(run)
    if not due:
        return NoDueRun.YET
    remaining = schedule.finish_runs(run.number for run in due)
    following = remaining.next_run()
    cutoff = instant - normal_window
    missed = min(len(due) - 1, sum(run.run.start.astimezone(UTC) < cutoff for run in due))
    return DueRuns(tuple(run.run for run in due[missed:]),
                   remaining if isinstance(following, SeriesRun) else NoSchedule.UNSET,
                   tuple(run.run for run in due[:missed]))


def _prepare_scheduled_runs(item: ListItem, moment: datetime, normal_window: timedelta) -> ListItem:
    for delivery in item.run_deliveries:
        confirmation = delivery.confirmation
        if isinstance(confirmation, AwaitingConfirmation) and moment >= confirmation.deadline:
            notices = (*delivery.notices, *(ScheduledNotice(kind) for kind in _TIMEOUT_NOTICES))
            expired = replace(delivery, confirmation=RunResult.NOT_CONFIRMED, notices=notices)
            item = _with_run_delivery(item, expired, moment)
    due = _due_schedule(item.schedule, moment, normal_window)
    if isinstance(due, NoDueRun):
        return item
    owner = item.owner
    if isinstance(owner, UnassignedOwner):
        print(f"the_list: {item.id}: assign a showrunner owner before this scheduled run starts", file=sys.stderr)
        return item
    missed = tuple(ItemChange(moment, f"Scheduled run due {_schedule_time_text(run.start)}. Missed.", "the List")
                   for run in due.missed)
    item = replace(item, state=ItemState.WORKING, schedule=due.remaining, log=(*item.log, *missed))
    for run in due.starts:
        delivery = RunDelivery(secrets.token_hex(16), run, item.title, owner, item.producer, len(item.log),
                               AwaitingInstruction.OWNER, tuple(ScheduledNotice(kind) for kind in _START_NOTICES))
        item = _with_run_delivery(item, delivery, moment)
    return item


def confirm_scheduled(keeper: staff.StaffId, identity: str, caller: staff.StaffRow, *, run_key: str = "") -> None:
    store = _store(keeper)
    with store.locked():
        item = load(keeper, identity)
        delivery = next((run for run in reversed(item.run_deliveries) if not run_key or run_key == run.key), None)
        if delivery is None:
            raise ListError("there is no matching scheduled run to confirm")
        if caller.id not in (delivery.owner, keeper):
            raise ListError("only the owner or morty can confirm a scheduled run")
        confirmation = delivery.confirmation
        moment = now()
        if confirmation is RunResult.CONFIRMED:
            return
        if confirmation is AwaitingInstruction.OWNER:
            raise ListError("the scheduled run is waiting for its owner instruction")
        if confirmation is RunResult.MANUALLY_SETTLED:
            raise ListError("a manual state change settled the scheduled run")
        if not isinstance(confirmation, AwaitingConfirmation) or moment >= confirmation.deadline:
            raise ListError("the confirmation time has passed")
        _save(store, _with_run_delivery(item, replace(delivery, confirmation=RunResult.CONFIRMED), moment))


def _confirm_on_natedev(identity: str, run_key: str) -> int:
    from scripts.lib import main_publish

    caller = shlex.quote(staff.caller_identity())
    args = shlex.join(["confirm", "--id", identity, "--run", run_key])
    command = (f'CODEX_THREAD_ID= CLAUDE_CODE_SESSION_ID={caller} "$HOME/.claude/scripts/lib/py" '
               f'"$HOME/.claude/scripts/production/the_list.py" {args}')
    limit = main_publish.CommandTimeLimit(conf.read("live_settings").whole("natedev_forward_seconds"))
    result = main_publish.ssh(command, ssh_program=os.environ.get("ZONE_SSH") or "ssh", host="natedev", time_limit=limit)
    if isinstance(result, str):
        raise ListError(f"natedev: {result}")
    print(result.stdout, end="")
    print(result.stderr, end="", file=sys.stderr)
    return result.returncode


def _scheduled_notice_command(identity: str, delivery: RunDelivery, notice: RunNotice) -> list[str]:
    due = _schedule_time_text(delivery.run.start)
    number = identity[1:]
    key = f"list-run:{identity}:{delivery.key}:{notice.name.lower()}"
    repeat = ("--repeat-minutes", "52560000")
    if notice in _START_NOTICES:
        heading = f"Scheduled List item {number} starts now: {delivery.title}"
        message = (f"{heading}\nDo this: {delivery.run.words}\nDue: {due}.\n"
                   f"Confirm with `{_LAUNCHER} confirm --id {identity} --run {delivery.key}`.")
    else:
        heading = f"Scheduled List item {number} has no confirmation"
        message = f"{heading}. {delivery.title} was due at {due}."
    if notice is RunNotice.USER_TIMEOUT:
        rendered = push_texts.render("nate.adhoc", sender="the List", title=heading, description=message)
        return [*push_texts.user_send_argv(rendered, key=key), "--user-asked", *repeat]
    recipient = delivery.owner if notice is RunNotice.OWNER_START else delivery.producer
    return session_send.send_argv("--from", "the List", "--key", key, *repeat,
                                  "--to", f"staff:{recipient}", "--text", message)


# Adopt and reap children that remain in the sender group after the sender exits.
_WAIT_FOR_SENDER_GROUP = r"""import os
import signal
import subprocess
import sys

sys.path.insert(0, sys.argv.pop(1))
from scripts.lib.descendants import adopt_descendants

if signal.getsignal(signal.SIGCHLD) == signal.SIG_IGN or not adopt_descendants():
    sys.exit("cannot reap the sender group")
report = int(sys.argv[1])
status = subprocess.call(sys.argv[2:])
os.write(report, f"{status} {os.fstat(1).st_size} {os.fstat(2).st_size}\n".encode())
os.close(report)
while True:
    try:
        os.waitpid(0, 0)
    except ChildProcessError:
        break
"""


@dataclass(frozen=True)
class ScheduledNoticeCapture:
    report: IO[bytes]
    output: IO[bytes]
    errors: IO[bytes]


def _send_scheduled_notice(identity: str, delivery: RunDelivery, notice: RunNotice,
                           capture: ScheduledNoticeCapture) -> None:
    budget = operation_deadline.remaining(_SCHEDULED_SEND_SECONDS)
    deadline = monotonic() + budget
    sender = _scheduled_notice_command(identity, delivery, notice)
    # Keep the sender output available even if a child keeps its descriptors open.
    report, output, errors = capture.report, capture.output, capture.errors
    command = ["timeout", "--signal=KILL", "--kill-after=1", str(budget), sys.executable, "-I", "-c",
               _WAIT_FOR_SENDER_GROUP, str(Path(__file__).resolve().parents[2]), str(report.fileno()), *sender]
    timed_out = False
    with subprocess.Popen(command, stdout=output, stderr=errors, start_new_session=True,
                          pass_fds=(report.fileno(),), env={**os.environ, "SEND_REPEAT_NOTICE_JOB": "1"}) as child:
        try:
            _ = child.wait(timeout=max(0, deadline - monotonic()))
        except BaseException as error:
            with suppress(ProcessLookupError):
                process_group.send_process_group(child.pid, signal.SIGKILL)
            if not isinstance(error, subprocess.TimeoutExpired):
                raise
            timed_out = True
    timed_out = timed_out or child.returncode in (124, -signal.SIGKILL, 128 + signal.SIGKILL)
    _ = report.seek(0)
    receipt = report.readline()
    _ = output.seek(0)
    _ = errors.seek(0)
    if not receipt.endswith(b"\n"):
        if timed_out:
            raise ListError("message delivery timed out")
        raise ListError(errors.read().decode("utf-8", "replace").strip() or "the sender did not report its result")
    status, output_size, error_size = map(int, receipt.split())
    stdout = output.read(output_size).decode("utf-8", "replace")
    stderr = errors.read(error_size).decode("utf-8", "replace")
    if timed_out:
        print(f"the_list: {identity}: {notice}: sender group timed out after the sender exited", file=sys.stderr)
    attempt = send_result.send_outcome(status, stdout)
    if (isinstance(attempt, send_result.ConfirmedSendOutcome)
            and attempt.header.outcome in {"sent", "queued", "kept", "skipped", "replaced"}):
        return
    detail = (stderr or stdout).strip() or "message delivery failed"
    raise ListError(detail)


_SCHEDULED_SEND_SECONDS = 2.0
_SCHEDULED_ITEM_SECONDS = 3.0
_SCHEDULED_CHECK_SECONDS = 8.0
_SCHEDULED_CHECK_READY = "ready"
_SCHEDULED_CHECK_IDLE = "idle"
_SCHEDULED_CHECK_FIELDS = frozenset({"scheduled_check", "schedule", "run_delivery"})


def _next_scheduled_run(schedule: Schedule) -> ScheduledRun | repeat_rule.NoNextRun:
    if isinstance(schedule, ScheduledSeries):
        pending = schedule.next_run()
        return pending.run if isinstance(pending, SeriesRun) else pending
    return schedule if isinstance(schedule, ScheduledRun) else repeat_rule.NoNextRun.REMAINING


def _legacy_schedule_due(store: record_store.NumberedRecords, path: Path,
                         fields: dict[str, list[str]], moment: datetime) -> bool:
    legacy = fields.get("run_delivery")
    if legacy:
        stored = cast(object, json.loads(store.field(path.stem, "run_delivery")))
        if stored is not None:
            delivery = _read_run_delivery(stored)
            if any(notice.needs_check() for notice in delivery.notices):
                return True
            confirmation = delivery.confirmation
            if isinstance(confirmation, AwaitingConfirmation) and confirmation.deadline <= moment:
                return True
    if not fields.get("schedule"):
        return False
    stored_schedule = cast(object, json.loads(store.field(path.stem, "schedule")))
    next_run = _next_scheduled_run(_read_schedule(stored_schedule, series_run_limit))
    return isinstance(next_run, ScheduledRun) and next_run.start.astimezone(UTC) <= moment.astimezone(UTC)


def _scheduled_check(item: ListItem) -> ScheduledCheckState | ScheduledCheckAt:
    """Find the first time that the scheduler needs the record."""
    if any(notice.needs_check() for run in item.run_deliveries for notice in run.notices):
        return ScheduledCheckState.READY
    times = [run.confirmation.deadline for run in item.run_deliveries
             if isinstance(run.confirmation, AwaitingConfirmation)]
    next_run = _next_scheduled_run(item.schedule)
    if isinstance(next_run, ScheduledRun):
        times.append(next_run.start)
    if times:
        return ScheduledCheckAt(min(times, key=lambda instant: instant.astimezone(UTC)))
    return ScheduledCheckState.IDLE


def _change_notice(delivery: RunDelivery, kind: RunNotice, state: NoticeState) -> RunDelivery:
    return replace(delivery, notices=tuple(replace(notice, state=state) if notice.kind is kind else notice
                                          for notice in delivery.notices))


def _settle_notice(store: record_store.NumberedRecords, item: ListItem, delivery: RunDelivery, kind: RunNotice,
                   state: NoticeState, wait: timedelta) -> None:
    moment = now()
    delivery = _change_notice(delivery, kind, state)
    if kind is RunNotice.OWNER_START and delivery.confirmation is AwaitingInstruction.OWNER:
        delivery = replace(delivery, confirmation=AwaitingConfirmation(moment + wait))
    uncertain = UncertainNotice(kind) if state is NoticeState.UNCERTAIN else DeliveryUncertainty.NONE
    _save(store, _with_run_delivery(item, delivery, moment, uncertain=uncertain))


def _deliver_scheduled_notice(store: record_store.NumberedRecords, identity: str, run_key: str,
                              kind: RunNotice, wait: timedelta) -> None:
    operation_deadline.check()
    with ExitStack() as capture_files:
        with exclusive(store.lock_path(), wait=False) as lock:
            if lock is LockResult.BUSY:
                return
            item = load(store.owner, identity)
            delivery = next((run for run in item.run_deliveries if run.key == run_key), None)
            if delivery is None:
                return
            notice = next(entry for entry in delivery.notices if entry.kind is kind)
            if notice.state is NoticeState.SENDING:
                _settle_notice(store, item, delivery, kind, NoticeState.UNCERTAIN, wait)
                return
            if notice.state is not NoticeState.READY:
                return
            capture = ScheduledNoticeCapture(capture_files.enter_context(TemporaryFile()),
                                             capture_files.enter_context(TemporaryFile()),
                                             capture_files.enter_context(TemporaryFile()))
            operation_deadline.check()
            _save(store, _with_run_delivery(item, _change_notice(delivery, kind, NoticeState.SENDING), now()))
        result = NoticeState.ACCEPTED
        try:
            _send_scheduled_notice(identity, delivery, kind, capture)
        except (ListError, OSError, subprocess.SubprocessError) as error:
            print(f"the_list: {identity}: {kind}: {error}", file=sys.stderr)
            result = NoticeState.UNCERTAIN
    with exclusive(store.lock_path(), wait=False) as lock:
        if lock is LockResult.BUSY:
            return
        item = load(store.owner, identity)
        current = next((run for run in item.run_deliveries if run.key == run_key), None)
        if current is not None and ScheduledNotice(kind, NoticeState.SENDING) in current.notices:
            _settle_notice(store, item, current, kind, result, wait)


def _fire_scheduled_item(store: record_store.NumberedRecords, identity: str, moment: datetime,
                         normal_window: timedelta) -> None:
    with exclusive(store.lock_path(), wait=False) as lock:
        if lock is LockResult.BUSY:
            return
        item = load(store.owner, identity)
        prepared = _prepare_scheduled_runs(item, moment, normal_window)
        if prepared != item:
            _save(store, prepared)
    if any(notice.needs_check() for run in prepared.run_deliveries for notice in run.notices):
        wait = _confirmation_wait()
        try:
            with operation_deadline.within(monotonic() + _SCHEDULED_ITEM_SECONDS):
                for delivery in prepared.run_deliveries:
                    for notice in delivery.notices:
                        if notice.needs_check():
                            _deliver_scheduled_notice(store, identity, delivery.key, notice.kind, wait)
        except TimeoutError:
            operation_deadline.check()


def fire_scheduled(interval: timedelta) -> None:
    """Process due List runs under the natedev upkeep job lock."""
    if remote.local_machine() != staff.OWNER_MACHINE:
        return
    deadline = monotonic() + operation_deadline.remaining(_SCHEDULED_CHECK_SECONDS)
    with operation_deadline.within(deadline):
        keeper = producer_id(staff.snapshot(), "morty")
        store = _store(keeper)
        cursor = store.folder() / ".scheduled-scan"
        try:
            previous = cursor.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            previous = ""
        paths = sorted(store.folder().glob("L*.md"))
        offset = bisect_right(paths, previous, key=lambda path: path.name)
        last_inspected = previous
        for path in (*paths[offset:], *paths[:offset]):
            try:
                operation_deadline.check()
                last_inspected = path.name
                fields = record_store.read_header(path, keys=_SCHEDULED_CHECK_FIELDS)
                check = fields.get("scheduled_check", [])
                moment = now()
                if check == [_SCHEDULED_CHECK_IDLE]:
                    continue
                if check:
                    if check != [_SCHEDULED_CHECK_READY] and _required_time(check[0]) > moment:
                        continue
                elif not _legacy_schedule_due(store, path, fields, moment):
                    continue
                _fire_scheduled_item(store, path.stem, moment, 2 * interval)
            except TimeoutError:
                if last_inspected != previous:
                    write_text_atomic(cursor, last_inspected + "\n")
                break
            except (ValueError, OSError) as error:
                print(f"the_list: {path.stem}: {error}", file=sys.stderr)


def save_page_schedule(keeper: staff.StaffId, identity: str, operation: ScheduleOperation,
                       edits: ScheduleChanges, *, scope: ScheduleScope, expected: ScheduleExpectation,
                       repeat_zone: ZoneInfo, receipt: PageRequest) -> ListEdit:
    """Save the schedule and the page receipt in one write."""
    def edit(item: ListItem, moment: datetime) -> ListItem:
        if item.state is ItemState.CLOSED:
            raise ListError("Reopen the item before changing its schedule.")
        existing = item.schedule
        if operation is not ScheduleOperation.EDIT and item.state is not ItemState.SCHEDULED:
            raise ListError("This item has no active schedule.")
        if operation is ScheduleOperation.REMOVE:
            remaining = _remove_schedule(existing, scope, expected)
            state = ItemState.SCHEDULED if isinstance(remaining, ScheduledSeries) else ItemState.WORKING
            return replace(item, schedule=remaining, state=state)
        if operation is ScheduleOperation.START_NOW:
            schedule = _schedule_edit(existing, {"start": moment}, scope, moment,
                                      repeat_zone=repeat_zone, expected=expected)
        else:
            if not edits.get("words", "").strip():
                raise question_record.AnswerRefused("Enter an instruction for the start time.")
            if "start" in edits and edits["start"] <= moment:
                raise question_record.AnswerRefused("Choose a future start time.")
            schedule = _schedule_edit(existing, edits, scope, moment, repeat_zone=repeat_zone, expected=expected)
        _check_series_limit(schedule)
        return replace(item, state=ItemState.SCHEDULED, schedule=schedule, parked_until=NoTimestamp.UNSET)

    return _change(keeper, identity, edit, FieldChange("Updated", {}), PAGE_SOURCE, page_action=receipt)


def _say_schedule(schedule: Schedule) -> None:
    if isinstance(schedule, ScheduledSeries):
        print("Repeats: " + schedule.describe(), flush=True)
    elif isinstance(schedule, ScheduledRun):
        print("Does not repeat", flush=True)


def update(keeper: staff.StaffId, identity: str, changes: ItemChanges, *, source: str) -> ListItem:
    return _update(keeper, identity, changes, source=source, schedule_edits={}).item


_LAUNCHER = "~/.claude/scripts/lib/py ~/.claude/scripts/production/the_list.py"
_PRODUCER_UPDATE_FIELDS = ("title", "done_when", "unit", "latest_status", "next_step", "eta", "state", "next_check",
                           "evidence", "add_evidence")
_PRODUCER_SCHEDULE_FIELDS = ("start", "do", "repeat", "scope")
_PRODUCER_UPDATE_OPTIONS = tuple("--" + field.replace("_", "-") for field in _PRODUCER_UPDATE_FIELDS)
PRODUCER_SCHEDULE_UPDATE_OPTIONS = (*_PRODUCER_UPDATE_OPTIONS, *("--" + field for field in _PRODUCER_SCHEDULE_FIELDS))


def _check_producer_update_fields(fields: Iterable[str]) -> None:
    refused_fields = set(fields).difference(_PRODUCER_UPDATE_FIELDS, _PRODUCER_SCHEDULE_FIELDS)
    refused = sorted("--" + field.replace("_", "-") for field in refused_fields)
    if refused:
        raise ListError(f"producers can update only {', '.join(_PRODUCER_UPDATE_OPTIONS)}. "
                        + f"Refused options: {', '.join(refused)}")


def _append_evidence(evidence: str, line: str) -> str:
    separator = "\n" if evidence and not evidence.endswith("\n") else ""
    return evidence + separator + line


def log_field_changes(item: ListItem) -> Generator[dict[str, object]]:
    """Replay the saved fields and yield the fields to show for each log entry."""
    previous: dict[str, object] = {}
    for entry in item.log:
        event = entry.what
        shown: dict[str, object] = {}
        if isinstance(event, FieldChange):
            fields: dict[str, object] = dict(event.fields)
            if event.action == "Added":
                previous = dict(_CREATION_DEFAULTS)
                _ = previous.pop("requested_by")
            if event.action == "Closed":
                previous["state"] = ItemState.CLOSED
            shown = fields
            if event.action == "Updated":
                shown = {field: value for field, value in fields.items()
                         if field not in previous or previous[field] != value}
                evidence, old_evidence = shown.get("evidence"), previous.get("evidence")
                if isinstance(evidence, str):
                    old_lines: set[str] = set(old_evidence.splitlines()) if isinstance(old_evidence, str) else set()
                    new_lines = [line for line in evidence.splitlines() if line not in old_lines]
                    if new_lines:
                        shown["evidence"] = "\n".join(new_lines) or "\n"
                    elif evidence or not old_evidence:
                        del shown["evidence"]
            previous.update(fields)
        elif isinstance(event, Reopened) or event == "Reopened":
            previous.update(state=ItemState.NEW, closing_words="", parked_until=NoTimestamp.UNSET)
        elif isinstance(event, Unparked):
            previous["parked_until"] = NoTimestamp.UNSET
            if isinstance(event, UnparkedForDelivery):
                previous["state"] = ItemState.DELIVERED
        yield shown


def _update(keeper: staff.StaffId, identity: str, changes: ItemChanges, *, source: str,
            schedule_edits: ScheduleChanges, add_evidence: str = "",
            assigned_to: staff.StaffRow | AllItems = AllItems.INCLUDED,
            schedule_scope: ScheduleScope = ScheduleScope.UNSPECIFIED,
            say_schedule: bool = False) -> ListEdit:
    scope: staff.StaffId | AllItems = AllItems.INCLUDED
    if assigned_to is not AllItems.INCLUDED:
        _check_producer_update_fields(changes)
        if assigned_to.kind != "producer":
            raise ListError("the assigned producer has no producer staff row")
        if isinstance(assigned_to.retirement, staff.RetiredProducer):
            raise ListError(f"{assigned_to.name}: retired producer. Choose an active producer.")
        source = assigned_to.name
        scope = assigned_to.id

    def read_reference(number: int) -> ListItem:
        return load(keeper, f"L{number}")

    def require_related(number: int) -> None:
        _ = read_reference(number)

    def edit(item: ListItem, moment: datetime) -> ListItem:
        _open(item)
        state = changes.get("state", item.state)
        if state is ItemState.CLOSED:
            raise ListError("use close to record closing words and a time")
        changed = replace(item, **changes)
        if schedule_edits:
            if state is not ItemState.SCHEDULED:
                raise ListError("use --state Scheduled with schedule fields")
            schedule = _schedule_edit(item.schedule, schedule_edits, schedule_scope, moment,
                                      repeat_zone=RepeatInSelectedRunZone.KEEP)
            changed = replace(changed, schedule=schedule)
        elif ("state" in changes and state is not ItemState.SCHEDULED
              and (schedule_scope is not ScheduleScope.UNSPECIFIED
                   or (item.state is ItemState.SCHEDULED and not item.run_deliveries))):
            schedule = _remove_schedule(item.schedule, schedule_scope)
            changed = (replace(changed, state=ItemState.SCHEDULED, schedule=schedule)
                       if isinstance(schedule, ScheduledSeries) else replace(changed, schedule=schedule))
        elif schedule_scope is not ScheduleScope.UNSPECIFIED:
            raise ListError("--scope needs a schedule change or removal")
        elif "schedule" in changes:
            _require_schedule_selection(item.schedule, schedule_scope)
        if changed.state is ItemState.SCHEDULED:
            changed = replace(changed, parked_until=NoTimestamp.UNSET)
        if isinstance(item.report, ListReport) and changed.state != item.state:
            transitions = {ItemState.NEW: ItemState.WORKING, ItemState.WORKING: ItemState.REPAIRED}
            if changed.state != transitions.get(item.state):
                raise ListError("Use Works or Not fixed on the List to check a Repaired item.")
            if changed.state == ItemState.REPAIRED:
                if "next_step" not in changes:
                    raise ListError("Set --next-step to one line on how to try the repair.")
                changed = replace(changed, parked_until=NoTimestamp.UNSET)
        if add_evidence:
            changed = replace(changed, evidence=_append_evidence(changed.evidence, add_evidence))
        _validate(changed)
        _check_links(changed.number, changes, require_related=require_related, read_dependency=read_reference)
        if _schedule_fields(changed.schedule) != _schedule_fields(item.schedule):
            if schedule_edits or "schedule" in changes:
                _check_series_limit(changed.schedule)
            if say_schedule:
                _say_schedule(changed.schedule)
        return changed

    def event(changed: ListItem) -> FieldChange:
        fields: LogFields = {**changes}
        if "state" in fields:
            fields["state"] = changed.state
        if add_evidence:
            fields["evidence"] = changed.evidence
        return FieldChange("Updated", fields)
    return _change(keeper, identity, edit, event, source, assigned_to=scope)


def move_open_producer(keeper: staff.StaffId, identity: str, previous: staff.StaffId,
                       producer: staff.StaffId, *, source: str) -> ListItem:
    """Move an open item only while it belongs to the previous producer."""
    def edit(item: ListItem, _: datetime) -> ListItem:
        if item.state == ItemState.CLOSED or item.producer != previous:
            return item
        return replace(item, producer=producer)

    return _change(keeper, identity, edit, FieldChange("Updated", {"producer": producer}), source).item


@contextmanager
def require_no_open_producer_items(keeper: staff.StaffId, producer: staff.StaffId) -> Generator[None]:
    """Hold the List lock after confirming the producer has no open items."""
    store = _store(keeper)
    with store.locked():
        for path in store.folder().iterdir():
            if path.suffix == ".md" and store.valid_id(path.stem):
                item = load(keeper, path.stem)
                if item.state != ItemState.CLOSED and item.producer == producer:
                    raise ListError("new open items belong to the producer. Run agents retire again.")
        yield


def link(keeper: staff.StaffId, identity: str, reviews: Sequence[str], *, source: str) -> ListItem:
    """Add unique review keys after the command resolves the requested reviews."""
    chosen = tuple(dict.fromkeys(reviews))
    _validate_review_keys(chosen)
    def edit(item: ListItem, _: datetime) -> ListItem:
        _open(item)
        return replace(item, reviews=tuple(dict.fromkeys((*item.reviews, *chosen))))
    return _change(keeper, identity, edit, ReviewLinksChanged("Linked reviews", chosen), source).item


def unlink(keeper: staff.StaffId, identity: str, reviews: Sequence[str], *, source: str) -> ListItem:
    """Remove review keys while retaining the order of the other keys."""
    chosen = tuple(dict.fromkeys(reviews))
    _validate_review_keys(chosen)
    def edit(item: ListItem, _: datetime) -> ListItem:
        _open(item)
        return replace(item, reviews=tuple(review for review in item.reviews if review not in chosen))
    return _change(keeper, identity, edit, ReviewLinksChanged("Unlinked reviews", chosen), source).item


def park(keeper: staff.StaffId, identity: str, until: Parking, *, source: str,
         notice: PageItemNotice | NoPageRequest = NoPageRequest.ABSENT,
         check_deadline: Callable[[datetime], None] | None = None,
         assigned_to: staff.StaffId | AllItems = AllItems.INCLUDED,
         schedule_scope: ScheduleScope = ScheduleScope.UNSPECIFIED,
         expected_schedule: ScheduleExpectation | UseCurrentSchedule = UseCurrentSchedule.AT_WRITE) -> ListItem:
    fields: LogFields = {"parked_until": until}
    event = FieldChange("Parked", fields)
    if isinstance(until, datetime):
        _validate_time(until)
    if isinstance(notice, PageItemNotice) and until is not NoTimestamp.UNSET:
        notice = replace(notice, parked_until=_request_parking(until))
        event = PageParkingChange("Parked", fields, notice.request)

    def edit(item: ListItem, moment: datetime) -> ListItem:
        if isinstance(notice, PageItemNotice) and schedule_scope is ScheduleScope.THIS:
            raise question_record.AnswerRefused("Choose This and following runs or All runs to park a repeated item.")
        if check_deadline is not None:
            check_deadline(moment)
        elif isinstance(notice, PageItemNotice):
            require_active_parking(until, moment)
        _open(item)
        if until is ParkUntilDelivered.DELIVERED and item.state in (ItemState.DELIVERED, ItemState.DONE):
            raise ListError("This item is already delivered.")
        schedule = _remove_schedule(item.schedule, schedule_scope, expected_schedule)
        if isinstance(schedule, ScheduledSeries):
            return replace(item, schedule=schedule)
        state = ItemState.WORKING if item.state is ItemState.SCHEDULED else item.state
        return replace(item, state=state, schedule=schedule, parked_until=until)

    def parking_event(changed: ListItem) -> FieldChange:
        return FieldChange("Updated", {}) if isinstance(changed.schedule, ScheduledSeries) else event

    return _change(keeper, identity, edit, parking_event, source, page_action=notice, assigned_to=assigned_to).item


def unread_delivery(item: ListItem) -> bool:
    if item.state != ItemState.DELIVERED or not isinstance(item.unread_since, datetime):
        return False
    for change in reversed(item.log):
        event = change.what
        if isinstance(event, (MarkedRead, Reopened)) or event in ("Marked read", "Reopened"):
            break
        if isinstance(event, UnparkedForDelivery):
            return True
    return False


def unpark(keeper: staff.StaffId, identity: str, *, source: str,
           notice: PageItemNotice | NoPageRequest = NoPageRequest.ABSENT,
           assigned_to: staff.StaffId | AllItems = AllItems.INCLUDED) -> ListEdit:
    """Clear an active parking time and save its notice in the same write."""
    def edit(item: ListItem, moment: datetime) -> ListItem:
        if not parking_active(item.parked_until, moment):
            return item
        _open(item)
        return replace(item, parked_until=NoTimestamp.UNSET)
    return _change(keeper, identity, edit, Unparked(), source, page_action=notice, assigned_to=assigned_to)


@dataclass(frozen=True)
class ListCloseCaller:
    member: staff.StaffRow


def close(keeper: staff.StaffId, identity: str, words: str, *, source: str,
          notice: PageItemNotice | NoPageRequest = NoPageRequest.ABSENT,
          assigned_to: staff.StaffId | AllItems | ListCloseCaller = AllItems.INCLUDED,
          schedule_scope: ScheduleScope = ScheduleScope.UNSPECIFIED,
          expected_schedule: ScheduleExpectation | UseCurrentSchedule = UseCurrentSchedule.AT_WRITE) -> ListItem:
    _validate_closing_words(words)
    def edit(item: ListItem, moment: datetime) -> ListItem:
        if isinstance(item.question_kind, QuestionItemKind):
            native = question_item_exchanges(item)
            if (item.question_kind is not QuestionItemKind.TO_NATE or not isinstance(assigned_to, ListCloseCaller)
                    or not native or _native_prompt(native).asker != assigned_to.member.id):
                raise ListError(_QUESTION_CLOSE_REFUSAL)
            _require_question_move(item, QuestionMove.ASKER_CLOSE, ItemState.CLOSED)
        elif isinstance(assigned_to, ListCloseCaller):
            caller = assigned_to.member
            if caller.kind != "producer" or item.producer != caller.id:
                if caller.kind != "producer":
                    raise ListError("the current session must be a producer")
                raise ListError(f"{identity} is assigned to another producer. A producer can change only its own items.")
        schedule = _remove_schedule(item.schedule, schedule_scope, expected_schedule, words=words)
        if isinstance(schedule, ScheduledSeries):
            return replace(item, schedule=schedule)
        if item.state == ItemState.CLOSED:
            if item.closing_words == words:
                return item
            raise ListError("the item is already Closed with different closing words")
        answered = tuple(question for question in item.questions if question.answer is not None)
        closed_questions = tuple(ItemChange(moment, QuestionClosed(question), source)
                                 for question in item.questions if question.answer is None)
        return replace(item, state=ItemState.CLOSED, closed=moment, closing_words=words,
                       parked_until=NoTimestamp.UNSET, questions=answered, log=(*item.log, *closed_questions),
                       schedule=schedule)

    def close_event(changed: ListItem) -> FieldChange:
        if changed.state is not ItemState.CLOSED:
            return FieldChange("Updated", {})
        return FieldChange("Closed", {"closing_words": words})

    producer_access = AllItems.INCLUDED if isinstance(assigned_to, ListCloseCaller) else assigned_to
    return _change(keeper, identity, edit, close_event, source,
                   unread=False, page_action=notice, assigned_to=producer_access, question_move=QuestionMove.ASKER_CLOSE).item


def reopen(keeper: staff.StaffId, identity: str, *, source: str,
           assigned_to: staff.StaffId | AllItems = AllItems.INCLUDED) -> ListItem:
    def edit(item: ListItem, _: datetime) -> ListItem:
        if item.state != ItemState.CLOSED:
            return item
        return replace(item, state=ItemState.NEW, closed=NoTimestamp.UNSET, closing_words="", parked_until=NoTimestamp.UNSET)
    return _change(keeper, identity, edit, Reopened(), source, assigned_to=assigned_to).item


def _unread_log_content(content: LogContent) -> str | dict[str, object]:
    if isinstance(content, LegacyFieldChange):
        return content.note
    return content if isinstance(content, str) else _log_content(content)


_WORKING_CHANGE = FieldChange("Updated", {"state": ItemState.WORKING})


def _start_working(item: ListItem, moment: datetime, source: str) -> ListItem:
    entry = ItemChange(moment, _WORKING_CHANGE, source)
    return replace(item, state=ItemState.WORKING, log=(*item.log, entry))


def _is_tell_move(log: tuple[ItemChange, ...], index: int) -> bool:
    """Identify the state change saved with the Tell feedback."""
    if log[index].what != _WORKING_CHANGE or index + 1 == len(log):
        return False
    feedback = log[index + 1]
    return (feedback.source == PAGE_SOURCE and feedback.time == log[index].time
            and isinstance(feedback.what, PageFeedback) and feedback.what.action == "tell")


def unread_update(item: ListItem) -> UnreadUpdate | NoUnreadUpdate:
    """Keep an update identity stable when page feedback adds log entries."""
    if not isinstance(item.unread_since, datetime):
        return NoUnreadUpdate.ABSENT
    changes = [(index, entry.time.isoformat(), _unread_log_content(entry.what), entry.source) for index, entry in enumerate(item.log)
               if not (_own_page_entry(entry) or _is_tell_move(item.log, index))]
    text = json.dumps((item.unread_since.isoformat(), changes), ensure_ascii=False)
    return UnreadUpdate(hashlib.sha256(text.encode()).hexdigest())


def _own_page_entry(entry: ItemChange) -> bool:
    """Find nate's own page entries. They do not change the unread update."""
    if entry.source == _IMAGE_ANSWER_SOURCE:
        return True
    return entry.source == PAGE_SOURCE and (isinstance(entry.what, MarkedRead) or entry.what == "Marked read"
                                            or _is_feedback(entry) or is_page_request_receipt(entry))


def _unread_changes(item: ListItem, *, include_park_receipts: bool = False) -> Generator[ItemChange]:
    """Give unread changes in reverse order. Exclude page feedback and its Tell state change.

    A read or a close ends the walk. A reopen is the last change that the walk gives.
    """
    if not isinstance(item.unread_since, datetime):
        return
    for index in range(len(item.log) - 1, -1, -1):
        change = item.log[index]
        content = change.what
        if (isinstance(content, MarkedRead) or content == "Marked read"
                or isinstance(content, FieldChange) and content.action == "Closed"):
            return
        park_receipt = include_park_receipts and isinstance(content, PageRequest) and content.action == "park"
        if park_receipt or not (_own_page_entry(change) or _is_tell_move(item.log, index)):
            yield change
        if isinstance(content, Reopened) or content == "Reopened":
            return


def unread_answer_ids(item: ListItem) -> frozenset[UnreadAnswerId]:
    """Find unread answers after accounting for parking actions."""
    read_times: list[datetime] = []
    answers: set[UnreadAnswerId] = set()
    for change in _unread_changes(item, include_park_receipts=True):
        content = change.what
        if isinstance(content, (QuestionAnswered, ReturnedAnswer)):
            if question_path.answer_is_unread(change.time, read_times):
                answers.add(content.question_id if isinstance(content, QuestionAnswered) else content.identity)
        elif (isinstance(content, FieldChange) and content.action == "Parked"
              and content.fields.get("parked_until", NoTimestamp.UNSET) is not NoTimestamp.UNSET
              or isinstance(content, PageRequest) and content.action == "park"):
            read_times.append(change.time)
    return frozenset(answers)


def _clear_unread(item: ListItem) -> ListItem:
    return replace(item, unread_since=NoTimestamp.UNSET)


def mark_read(keeper: staff.StaffId, identity: str, *, source: str,
              update: UnreadUpdate | NoUnreadUpdate | ReadCurrentUpdate = ReadCurrentUpdate.CURRENT,
              page_request: PageRequest | NoPageRequest = NoPageRequest.ABSENT) -> ListItem:
    return mark_read_edit(keeper, identity, source=source, update=update, page_request=page_request).item


def mark_read_edit(keeper: staff.StaffId, identity: str, *, source: str,
                   update: UnreadUpdate | NoUnreadUpdate | ReadCurrentUpdate,
                   page_request: PageRequest | NoPageRequest) -> ListEdit:
    """Return the result and the item read under the write lock.

    A List page Mark read on an item that waits for nate moves it to Working.
    The same write saves a notice that asks the item's producer for the next step.
    """
    def edit(item: ListItem, moment: datetime) -> ListItem:
        if question_item(item) is not NoQuestionItem.ORDINARY:
            if item.state is not ItemState.ANSWERED or (
                    update is not ReadCurrentUpdate.CURRENT and unread_update(item) != update):
                raise ListError("The question changed. Check it again before marking it read.")
            if isinstance(page_request, PageRequest) and page_request.words:
                if isinstance(item.owner, UnassignedOwner):
                    raise ListError("The question has no owner for your words.")
                console_text.require(page_request.words, "list field", refusal=ListError)
            state = ItemState.WORKING if item.question_kind is QuestionItemKind.TO_NATE else ItemState.CLOSED
            return _move_question(item, QuestionMove.READ, state, moment, source)
        if update is not ReadCurrentUpdate.CURRENT and unread_update(item) != update:
            return item
        read = _clear_unread(item)
        if not isinstance(page_request, PageRequest) or not _waits_for_nate(item, moment):
            return read
        return _start_working(read, moment, source)
    return _change(keeper, identity, edit, MarkedRead(), source, unread=False, page_action=page_request,
                   question_move=QuestionMove.READ)


def add_ask(keeper: staff.StaffId, identity: str, *, kind: AskKind, target: staff.StaffId, what: str, why: str,
            config: bool, requester: staff.StaffId, source: str) -> Ask:
    """Open an ask for nate; the open ask makes the item unread."""
    _line(what, "--what")
    _line(why, "--why")
    _ = staff_id(target, "target")

    def edit(item: ListItem, moment: datetime) -> ListItem:
        _open(item)
        if requester not in (keeper, item.producer):
            raise ListError("only the item's producer or morty can open an ask")
        ask = Ask(f"A{len(item.asks) + 1}", kind, what, why, target, config, moment)
        return replace(item, asks=(*item.asks, ask))
    return _change(keeper, identity, edit, lambda changed: AskOpened(changed.asks[-1]), source).item.asks[-1]


def withdraw_ask(keeper: staff.StaffId, identity: str, ask_id: str, reason: str, *, caller: staff.StaffRow) -> None:
    """Close an open ask and record the reason under the item lock."""
    reason = reason.strip()
    _line(reason, "--reason")
    console_text.require(reason, "list field", refusal=ListError)

    def edit(item: ListItem, moment: datetime) -> ListItem:
        if caller.id not in (keeper, item.producer):
            raise ListError("only the item's producer or morty can withdraw an ask")
        if not any(ask.ask_id == ask_id for ask in open_asks(item)):
            raise ListError(f"{identity} has no open ask {ask_id}")
        return replace(item, asks=tuple(replace(ask, closed=moment, withdrawn=True) if ask.ask_id == ask_id else ask
                                        for ask in item.asks))

    def event(changed: ListItem) -> AskWithdrawn:
        return AskWithdrawn(next(ask for ask in changed.asks if ask.ask_id == ask_id), reason)

    _ = _change(keeper, identity, edit, event, caller.name)

def _close_asks(item: ListItem, ask_ids: set[str], moment: datetime, reference: str) -> ListItem:
    return replace(item, asks=tuple(replace(ask, closed=moment, go_id=reference) if ask.ask_id in ask_ids else ask
                                    for ask in item.asks))


def give_go(keeper: staff.StaffId, identity: str, ask_id: str, go_id: str, *, source: str) -> ListEdit:
    """Close the ask with the go id; mark the item read when no answerable ask stays open.

    When the Go closes the last answerable ask of an item that waits for nate, the item moves to Working.
    The same write saves a notice that asks the item's producer for the next step, as Mark read does.
    """
    def edit(item: ListItem, moment: datetime) -> ListItem:
        _open(item)
        if not any(ask.ask_id == ask_id for ask in open_asks(item)):
            raise ListError("This ask is no longer open.")
        closed = _close_asks(item, {ask_id}, moment, go_id)
        return replace(closed, state=ItemState.WORKING) if _waits_for_nate(closed, moment) else closed

    def event(changed: ListItem) -> GoGiven:
        return GoGiven(next(ask for ask in changed.asks if ask.ask_id == ask_id))

    def stays_unread(changed: ListItem) -> bool:
        # Another open ask or an unread change other than an ask or a Go keeps the item unread.
        return bool(answerable_asks(changed, staff.snapshot())) or any(
            not isinstance(change.what, (AskOpened, GoGiven)) for change in _unread_changes(changed))

    def working(item: ListItem, changed: ListItem, moment: datetime) -> tuple[ItemChange, ...]:
        if changed.state == item.state:
            return ()
        state = ItemChange(moment, FieldChange("Updated", {"state": ItemState.WORKING}), source)
        # As with Mark read, the read comes after the state change, so a later unread walk stops before it.
        read = () if stays_unread(changed) else (ItemChange(moment, MarkedRead(), PAGE_SOURCE),)
        notice = _next_step_notice(keeper, go_next_step_request(go_id), "nate gave the Go on", changed)
        return (state, *read, ItemChange(moment, notice, PAGE_SOURCE))
    before: list[ListItem] = []

    def save(item: ListItem, moment: datetime) -> ListItem:
        before.append(item)
        return edit(item, moment)
    try:
        return _change(keeper, identity, save, event, source, unread=stays_unread, follow=working)
    except AtomicDurabilityUnconfirmed:
        # The new content can be visible. Keep the Go when the saved ask holds this go id.
        try:
            published = load(keeper, identity)
        except (OSError, ValueError) as error:
            raise SavedReadFailed(f"the Go is saved; the item could not be read again: {error}") from error
        if not any(ask.ask_id == ask_id and ask.go_id == go_id and not ask.is_open for ask in published.asks):
            raise
        return SavedEdit(before[0] if before else published, published)


def log_go_not_delivered(keeper: staff.StaffId, identity: str, go_id: str, recipient: staff.StaffId) -> ListItem:
    """Add the log line that tells nate a Go message did not reach its recipient."""
    def edit(item: ListItem, _: datetime) -> ListItem:
        # The line changes no field. Clear the update time so that the save always differs; the save sets it again.
        return replace(item, updated=NoTimestamp.UNSET)

    def event(changed: ListItem) -> GoNotDelivered:
        ask = next((ask for ask in changed.asks if ask.go_id and ask.go_id == go_id), None)
        if ask is None:
            raise ListError("no closed ask has this go id")
        return GoNotDelivered(ask, recipient)
    return _change(keeper, identity, edit, event, "the List").item


def has_open_question(item: ListItem) -> bool:
    return any(question.answer is None for question in item.questions)


def answerable_asks(item: ListItem, members: staff.StaffSnapshot) -> tuple[Ask, ...]:
    """Find the open asks that nate can answer: the item is open and the ask's target has a staff row.

    The page shows only these asks, so only these asks keep an item waiting for nate.
    """
    if item.state == ItemState.CLOSED:
        return ()
    return tuple(ask for ask in open_asks(item) if members.by_id(ask.target) is not None)


def _waits_for_nate(item: ListItem, moment: datetime) -> bool:
    """Return True for an unparked item that waits for nate with no open question and no ask that nate can answer."""
    return (item.state == ItemState.WAITING_FOR_NATE and not has_open_question(item)
            and not answerable_asks(item, staff.snapshot()) and not parking_active(item.parked_until, moment))


_GO_NEXT_STEP = "-next-step"


def go_next_step_request(go_id: str) -> str:
    """Name the next-step notice of one Go. Its own name gives it a delivery key apart from the Go."""
    return go_id + _GO_NEXT_STEP


def next_step_go_id(request: str) -> str | None:
    """Find the go id of a Go's next-step notice, or None for each other request."""
    go_id = request.removesuffix(_GO_NEXT_STEP)
    return go_id if go_id != request else None


def _next_step_notice(keeper: staff.StaffId, request: str, done: str, item: ListItem, *, form_words: str = "") -> PageFeedback:
    """Ask the item's producer for the next step and save the producer as the recipient.

    `done` names what nate did, such as "nate read". A Mark read keeps its page words in `form_words`,
    so a retry matches the page request.
    """
    members = staff.snapshot()
    producer = members.by_id(item.producer)
    name = shlex.quote(producer.name if producer is not None else item.producer)
    default_keeper = record_store.list_keeper(conf.read_or_repository("console"), members)
    keeper_option = "" if default_keeper is not None and keeper == default_keeper.id else f" --keeper {shlex.quote(keeper)}"
    words = (f"Studio: {done} item {item.number}, so it is now Working. Write its next step: "
             + f"`{_LAUNCHER} update --id {item.id}{keeper_option} --source {name} --next-step \"<step>\"`.")
    return PageFeedback(request, words, action="read", form_words=form_words, recipient=item.producer)


def is_page_request_receipt(entry: ItemChange) -> bool:
    return entry.source == PAGE_SOURCE and (isinstance(entry.what, (PageRequest, QuestionRequestReceipt, UnparkNoChangeReceipt))
        or isinstance(entry.what, str) and entry.what.startswith(_LEGACY_RECEIPT))


def _is_feedback(entry: ItemChange) -> bool:
    return entry.source == PAGE_SOURCE and (isinstance(entry.what, PageFeedback)
        or isinstance(entry.what, str) and entry.what.startswith(_LEGACY_FEEDBACK))


def _validate_page_source(source: str) -> None:
    if source != PAGE_SOURCE:
        raise ListError("a List page request must use the List page source")


def _read_page_activity(fields: dict[str, object], source: str, *, receipt: bool) -> PageActivity:
    _validate_page_source(source)
    request = _request_id(fields.get("request"))
    words, form_words = fields.get("words"), fields.get("form_words", "")
    action = fields.get("action")
    if not isinstance(words, str) or not isinstance(form_words, str):
        raise ListError("invalid List request log")
    parking_time = fields.get("parked_until", "")
    if not isinstance(parking_time, str) or (parking_time and action != "park"):
        raise ListError("invalid List request parking time")
    parked_until = _request_parking(parking_value(parking_time))
    if "unpark_no_change" in fields and (not receipt or action != "unpark"):
        raise ListError("invalid Unpark receipt")
    if receipt:
        if action not in ("read", "remove", "park", "unpark", "question", "works"):
            raise ListError("invalid List request receipt")
        if action not in ("read", ReportVerdict.WORKS) and not words.strip():
            raise ListError("invalid List request receipt")
        received = PageRequest(request, ReportVerdict.WORKS if action == "works" else action,
                               words, form_words, parked_until)
        if action == "question":
            return QuestionRequestReceipt(received, _question_id(fields.get("question_id")))
        if "unpark_no_change" in fields:
            reason = fields["unpark_no_change"]
            if reason not in (UnparkNoChangeReason.EXPIRED, UnparkNoChangeReason.NO_ACTIVE_PARKING):
                raise ListError("invalid Unpark outcome")
            return UnparkNoChangeReceipt(received, UnparkNoChangeReason(reason))
        return received
    state, alerted_to, target = fields.get("state"), fields.get("alerted_to", ""), fields.get("read_update", "")
    if state not in ("pending", "sent", "alerted") or not isinstance(alerted_to, str) or not isinstance(target, str):
        raise ListError("invalid List feedback log")
    if action not in ("tell", "remove", "park", "unpark", "read", "not works") or not words.strip():
        raise ListError("invalid List feedback action")
    recipient = _field_text(fields.get("recipient", ""), "feedback recipient")
    additional = _field_text(fields.get("additional_recipient", ""), "additional feedback recipient")
    origin = PageNoticeOrigin(_field_text(fields.get("origin", PageNoticeOrigin.LIST_PAGE), "feedback origin"))
    return PageFeedback(request, words, state, alerted_to,
                        UnreadUpdate(target) if target else NoUnreadUpdate.ABSENT,
                        ReportVerdict.NOT_FIXED if action == "not works" else action,
                        form_words, parked_until,
                        recipient=staff_id(recipient, "feedback recipient") if recipient else LegacyFeedbackRecipient.KEEPER,
                        additional_recipient=(staff_id(additional, "additional feedback recipient")
                                              if additional else NoAdditionalRecipient.ABSENT), origin=origin)


def _page_requests(item: ListItem) -> dict[str, PageActivity]:
    requests: dict[str, PageActivity] = {}
    for entry in item.log:
        content = entry.what
        if entry.source == PAGE_SOURCE and isinstance(content, (PageRequest, PageFeedback, QuestionRequestReceipt, UnparkNoChangeReceipt)):
            identity = (content.request.request if isinstance(content, (QuestionRequestReceipt, UnparkNoChangeReceipt))
                        else content.request)
            requests[identity] = content
    return requests


def feedback_requests(item: ListItem) -> dict[str, PageFeedback]:
    """Read the latest delivery state for each request from the item log."""
    return {request: saved for request, saved in _page_requests(item).items() if isinstance(saved, PageFeedback)}


def has_park_receipt(item: ListItem, request: str) -> bool:
    """Check whether this exact Park request saved."""
    saved = _page_requests(item).get(_request_id(request))
    return isinstance(saved, (PageRequest, PageFeedback)) and saved.action == "park"


def remove_receipt(item: ListItem, request: str) -> PageRequest | PageFeedback | NoPageRequest:
    """Find the receipt even if Remove saved without a notice."""
    saved = _page_requests(item).get(_request_id(request))
    if isinstance(saved, (PageRequest, PageFeedback)) and saved.action == "remove":
        return saved
    return NoPageRequest.ABSENT


def unpark_no_change_reason(item: ListItem, request: str) -> UnparkNoChangeReason:
    """Read the original reason from an Unpark receipt."""
    saved = _page_requests(item).get(_request_id(request))
    if isinstance(saved, UnparkNoChangeReceipt):
        return saved.reason
    if isinstance(saved, PageRequest) and saved.action == "unpark":
        # Older receipts do not record why the request made no change.
        return UnparkNoChangeReason.NO_ACTIVE_PARKING
    raise ListError("the List item has no Unpark receipt for this request")


def _previous_page_request(
        item: ListItem, request: PageRequest) -> PageActivity | NoPageRequest:
    previous = _page_requests(item).get(_request_id(request.request), NoPageRequest.ABSENT)
    if previous is not NoPageRequest.ABSENT:
        if isinstance(previous, PageFeedback) and previous.action == "read":
            # A Mark read notice keeps the page words in form_words.
            key = PageRequest(previous.request, "read", previous.form_words)
        elif isinstance(previous, PageFeedback):
            key = previous.key()
        elif isinstance(previous, (QuestionRequestReceipt, UnparkNoChangeReceipt)):
            key = previous.request
        else:
            key = previous
        if key.action == request.action == "park" and request.parked_until is not NoTimestamp.UNSET:
            target = key.parked_until
            if target is NoTimestamp.UNSET:
                recorded = [change.fields.get("parked_until", NoTimestamp.UNSET) for entry in item.log
                            if isinstance(change := entry.what, PageParkingChange) and change.request == key.request]
                if len(recorded) == 1:
                    target = _request_parking(recorded[0])
            if target != request.parked_until:
                raise ListError("this request already has a different parking time")
            if key.form_words != request.form_words:
                raise ListError("this request already has a different action or words")
        elif key.action == request.action == "unpark":
            if key.form_words != request.form_words:
                raise ListError("this request already has a different action or words")
        elif key != request:
            raise ListError("this request already has a different action or words")
    return previous


def _save_page_request(store: record_store.NumberedRecords, keeper: staff.StaffId,
                       item: ListItem, request: PageRequest) -> ListItem:
    try:
        _save(store, item)
    except AtomicDurabilityUnconfirmed:
        published = load(keeper, item.id)
        if _previous_page_request(published, request) is NoPageRequest.ABSENT:
            raise
        return published
    return item


class NoQuestionAnswerer(StrEnum):
    ABSENT = "absent"


class _NoActiveMorty(Enum):
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class _QuestionAnswerer:
    """The caller and the active morty ID, if available."""
    member: staff.StaffRow
    morty: staff.StaffId | _NoActiveMorty


@dataclass(frozen=True)
class _ListQuestionStore:
    keeper: staff.StaffId
    ask_request: PageRequest | NoPageRequest = NoPageRequest.ABSENT
    answerer: _QuestionAnswerer | NoQuestionAnswerer = NoQuestionAnswerer.ABSENT

    def questions(self, item: str) -> tuple[question_record.Question, ...]:
        current = load(self.keeper, item)
        if isinstance(self.ask_request, PageRequest):
            # The shared allocator reserves IDs of resolved questions through its answer field.
            archived = tuple(replace(entry.what.question, answer=question_record.Answer(
                "Closed with the item.", entry.time.isoformat()))
                for entry in current.log if isinstance(entry.what, QuestionClosed))
            return (*current.questions, *archived)
        return current.questions

    def add(self, item: str, question: question_record.Question) -> None:
        # ask_question holds the List lock from the receipt check through this write.
        assert isinstance(self.ask_request, PageRequest)
        current = load(self.keeper, item)
        self.save_request(replace(current, questions=(*current.questions, question)), self.ask_request, question)

    def save_request(self, item: ListItem, request: PageRequest, question: question_record.Question) -> None:
        receipt = ItemChange(now(), QuestionRequestReceipt(request, question.question_id), PAGE_SOURCE)
        _ = _save_page_request(_store(self.keeper), self.keeper, replace(item, log=(*item.log, receipt)), request)

    def require_answerer(self, current: ListItem, question_id: str) -> _QuestionAnswerer:
        question = next((question for question in current.questions if question.question_id == question_id), None)
        if question is None or question.answer is not None:
            raise question_record.AnswerRefused("This item has no open question with that id.")
        answerer = self.answerer
        allowed = (self.keeper, question.recipient_id(self.keeper))
        if answerer is NoQuestionAnswerer.ABSENT or answerer.member.id not in (*allowed, answerer.morty):
            raise question_record.AnswerRefused("Only the question recipient or morty can answer.")
        return answerer

    def set_answer(self, item: str, question_id: str, answer: question_record.Answer) -> None:
        _ = self.require_answerer(load(self.keeper, item), question_id)
        console_text.require(answer.text, "reply", identity=f"list/{self.keeper}/{item}/{question_id}",
                             refusal=question_record.AnswerRefused)
        store = _store(self.keeper)
        with store.locked():
            current = load(self.keeper, item)
            answerer = self.require_answerer(current, question_id)
            questions = tuple(replace(saved, answer=answer) if saved.question_id == question_id else saved
                              for saved in current.questions)
            moment = now()
            entry = ItemChange(moment, QuestionAnswered(question_id), answerer.member.name)
            changed = replace(current, questions=questions, updated=moment, unread_since=moment, log=(*current.log, entry))
            if isinstance(current.question_kind, QuestionItemKind):
                changed = _move_question(changed, QuestionMove.REPLY, ItemState.ANSWERED, moment, answerer.member.name)
            try:
                _save(store, changed)
            except AtomicDurabilityUnconfirmed:
                if load(self.keeper, item) != changed:
                    raise
        file_exchange = cast(Callable[[staff.StaffId, ListItem], None],
                             import_module("scripts.review_links.answer_sources").file_saved_list)
        file_exchange(self.keeper, changed)


def resolve_question_recipient(keeper: staff.StaffId, producer: staff.StaffId
                               ) -> staff.StaffId | question_record.KeeperForUnavailableProducer:
    if producer == keeper:
        return keeper
    member = next((row for row in staff.active(staff.snapshot(), "producer") if row.id == producer), None)
    if member is not None and not isinstance(member.exited, staff.Exited):
        try:
            _ = session_pane.session_place(member.current_session)
        except (session_pane.NoSuchTarget, OSError, session_pane.HerdrFailure, ValueError):
            pass
        else:
            return producer
    return question_record.KeeperForUnavailableProducer(keeper, producer)


def _prepare_question_recipient(path: Path, request: PageRequest, keeper: staff.StaffId, producer: staff.StaffId,
                                recipient: question_record.QuestionRecipient
                                ) -> staff.StaffId | question_record.KeeperForUnavailableProducer:
    """Keep the first recipient if the List write fails."""
    fields = _request_fields(request)
    try:
        saved = delivery_state.read_fields(path)
    except FileNotFoundError:
        if recipient is question_record.DefaultQuestionRecipient.PAGE:
            recipient = resolve_question_recipient(keeper, producer)
        record = {"request": fields, **question_record.recipient_fields(recipient)}
        write_text_atomic(path, json.dumps(record) + "\n", new_file_mode=0o600)
        return recipient
    if saved.get("request") != fields:
        raise ListError("this request already has a different action or words")
    recipient = question_record.read_recipient(saved)
    if recipient is question_record.DefaultQuestionRecipient.PAGE:
        raise ListError("the prepared question has no recipient")
    return recipient


def ask_question(keeper: staff.StaffId, identity: str, request: PageRequest,
                 status: change_page.DeliveryStatus, *,
                 recipient: question_record.QuestionRecipient = question_record.DefaultQuestionRecipient.PAGE
                 ) -> change_page.MessageDelivery | change_page.NoMessageDelivery:
    """Save the question and its signed request receipt before delivery."""
    if request.action != "question" or not request.words.strip() or len(request.words) > 10000:
        raise question_record.AnswerRefused("Enter a question first.")
    request = replace(request, words=request.words.strip())
    store = _store(keeper)
    questions = _ListQuestionStore(keeper, request)
    with status.locked(), store.locked():
        item = load(keeper, identity)
        previous = _previous_page_request(item, request)
        if item.state == ItemState.CLOSED:
            if isinstance(previous, QuestionRequestReceipt):
                return change_page.NoMessageDelivery()
            raise question_record.AnswerRefused("This item is Closed.")
        open_question = next((question for question in item.questions
                              if question.answer is None and question.words == request.words), None)
        if isinstance(previous, QuestionRequestReceipt):
            if open_question is None or open_question.question_id != previous.question_id:
                return change_page.NoMessageDelivery()
        elif open_question is not None:
            if isinstance(recipient, question_record.KeeperForUnavailableProducer):
                recipient = recipient.keeper
            if (recipient is not question_record.DefaultQuestionRecipient.PAGE
                    and recipient != open_question.recipient_id(keeper)):
                raise ListError("This question already has a different recipient.")
            questions.save_request(item, request, open_question)
        elif item.question_kind is not NoQuestionItem.ORDINARY and item.state is not ItemState.WORKING:
            raise ListError("Ask the question while the item is Working.")
        request_key = json.dumps([str(store.path(item.id).resolve()), _request_id(request.request)]).encode()
        preparation = status.root / (".review-link-list-ask-" + hashlib.sha256(request_key).hexdigest())
        prepared = (_prepare_question_recipient(preparation, request, keeper, item.producer, recipient)
                    if open_question is None else open_question.recipient_id(keeper))
        delivery = _claim_question(questions, item, request.words, prepared, status)
        with suppress(OSError):
            preparation.unlink()
        return delivery


def _claim_question(questions: _ListQuestionStore, item: ListItem, words: str,
                    recipient: staff.StaffId | question_record.KeeperForUnavailableProducer,
                    status: change_page.DeliveryStatus) -> change_page.MessageDelivery:
    recipient_id = recipient.keeper if isinstance(recipient, question_record.KeeperForUnavailableProducer) else recipient
    target = list_question_target(questions.keeper, item, recipient_id)
    return page_question.ask(questions, replace(target, initial_recipient=recipient), words, status)


def list_question_target(keeper: staff.StaffId, item: ListItem,
                         recipient: staff.StaffId) -> change_page.QuestionDeliveryTarget:
    """Use the saved keeper and recipient for a List question."""
    owner = None if isinstance(item.owner, UnassignedOwner) else item.owner
    return change_page.list_question_target(_store(keeper).path(item.id), item.id, item.number, item.title,
                                           recipient, keeper=keeper, alert_owner=owner)


def image_list_keeper(identity: str) -> staff.StaffId:
    """Find the List folder that holds this item, including old folders."""
    if re.fullmatch(r"L[1-9][0-9]*", identity) is None:
        raise ListError("Use a List item id such as L324.")
    candidates = tuple(record_store.owner_state_paths(
        record_store.state_root(), f"list/{identity}.md", strict=True))
    if not candidates:
        raise ListError(f"No List item {identity}.")
    if len(candidates) != 1:
        raise ListError(f"More than one List folder holds {identity}.")
    keeper = staff.StaffId(candidates[0].parent.parent.name)
    _ = load(keeper, identity)
    return keeper


def save_image_answer(keeper: staff.StaffId, identity: str, key: str, words: str, *,
                      image_id: str, approved: bool) -> staff.StaffId:
    """Save the answer before delivery. Return the producer for this answer."""
    _ = _request_id(key)
    store = _store(keeper)
    with store.locked():
        item = load(keeper, identity)
        for entry in reversed(item.log):
            if isinstance(entry, _ImageAnswerChange) and entry.key == key:
                sync_folder(store.folder())
                return entry.producer
        moment = now()
        producer = staff.snapshot().by_id(item.producer)
        producer_name = producer.name if producer is not None else item.producer
        changes: ItemChanges = {}
        if item.state is not ItemState.REPAIRED and not (
                item.state is ItemState.DELIVERED and approved and isinstance(item.report, ListReport)):
            changes["next_step"] = f"{producer_name}: act on nate's answer to picture {image_id}"
        keeps_delivery = item.state is ItemState.DELIVERED and approved
        if item.state in (ItemState.WAITING_FOR_NATE, ItemState.DELIVERED) and not keeps_delivery:
            changes["state"] = ItemState.WORKING
        _require_question_move(item, QuestionMove.OTHER, changes.get("state", item.state))
        if item.eta is EtaStatus.WAITING_FOR_NATE and not keeps_delivery:
            changes["eta"] = EtaStatus.NO_TIME_YET
        picture = re.compile(r"(?<![a-z0-9])" + re.escape(image_id) + r"(?![a-z0-9])", re.IGNORECASE)
        asks = {ask.ask_id for ask in open_asks(item) if picture.search(ask.what) or picture.search(ask.why)}
        changed = _close_asks(_clear_unread(item), asks, moment, key)
        log = item.log
        if changes:
            log = (*log, ItemChange(moment, FieldChange("Updated", {**changes}), _IMAGE_ANSWER_SOURCE))
        if item.unread_since is not NoTimestamp.UNSET:
            log = (*log, ItemChange(moment, MarkedRead(), _IMAGE_ANSWER_SOURCE))
        answer = _ImageAnswerChange(moment, words, _IMAGE_ANSWER_SOURCE, key, item.producer)
        _save(store, replace(changed, **changes, updated=moment, log=(*log, answer)))
        return answer.producer


def save_feedback(keeper: staff.StaffId, identity: str, feedback: PageFeedback) -> PageFeedback:
    """Save pending feedback and any Tell state change in one locked write."""
    if not feedback.words.strip() or feedback.state != "pending":
        raise ListError("Enter a new request with feedback words.")
    store = _store(keeper)
    with store.locked():
        item = load(keeper, identity)
        request = feedback.key()
        previous = _previous_page_request(item, request)
        if isinstance(previous, PageFeedback):
            return previous
        if isinstance(previous, PageRequest):
            raise ListError("this request has no feedback to deliver")
        moment = now()
        if (feedback.action == "tell" and item.question_kind is NoQuestionItem.ORDINARY and not isinstance(item.report, ListReport)
                and item.state in (ItemState.WAITING_FOR_NATE, ItemState.DELIVERED)):
            item = _start_working(item, moment, PAGE_SOURCE)
        changed = replace(item, updated=moment, log=(*item.log, ItemChange(moment, feedback, PAGE_SOURCE)))
        _validate(changed)
        _ = _save_page_request(store, keeper, changed, request)
        return feedback


def deliver_feedback(keeper: staff.StaffId, identity: str, request: str,
                     send: Callable[[ListItem, PageFeedback], PageFeedback]) -> None:
    """Claim one request in this process and release the List lock before sending."""
    store = _store(keeper)
    claim = store.path(identity), request
    with feedback_claims_lock:
        if claim in feedback_claims:
            return
        feedback_claims.add(claim)
    try:
        with store.locked():
            item = load(keeper, identity)
            feedback = feedback_requests(item).get(request)
        if feedback is None or not (feedback.state == "pending"
                                    or (feedback.state == "alerted" and feedback.action is ReportVerdict.NOT_FIXED)):
            return
        result = send(item, feedback)
        if result == feedback or result.state == "pending":
            return
        with store.locked():
            current = load(keeper, identity)
            latest = feedback_requests(current).get(request)
            if latest is None or latest != feedback:
                return
            moment = now()
            if latest.action is ReportVerdict.NOT_FIXED:
                log = tuple(replace(entry, what=result) if entry.what == latest else entry for entry in current.log)
            else:
                log = (*current.log, ItemChange(moment, result, PAGE_SOURCE))
            _save(store, replace(current, updated=moment, log=log))
    finally:
        with feedback_claims_lock:
            feedback_claims.remove(claim)


def feedback_notice_time(item: ListItem) -> Timestamp:
    """Keep the feedback notice until the next writer adds a log entry."""
    for entry in reversed(item.log):
        if entry.source != PAGE_SOURCE:
            break
        if isinstance(entry.what, PageFeedback) and entry.what.state == "pending":
            return entry.time
    return NoTimestamp.UNSET


def order(item: ListItem) -> tuple[bool, bool, float, int]:
    """Put user decisions first, unread updates next, and early checks before late checks."""
    return (item.state != ItemState.WAITING_FOR_NATE, not isinstance(item.unread_since, datetime),
            item.next_check.timestamp() if isinstance(item.next_check, datetime) else float("inf"), item.number)


def table(keeper: staff.StaffId, *, include_closed: bool = False, include_parked: bool = False) -> str:
    return _format_table(records(keeper, include_closed=include_closed), staff.snapshot(), include_parked=include_parked)


def _format_table(items: Sequence[ListItem], record: staff.StaffSnapshot, *, include_parked: bool,
                  show_latest_status: bool = False) -> str:
    moment = now()
    today = moment.astimezone(user_zone()) if show_latest_status else moment
    visible = [item for item in items if include_parked or not parking_active(item.parked_until, moment)]
    visible.sort(key=order)
    columns = ("#", "Owner", "Producer", "Item", "State", "Next step", "ETA", "Update")
    if show_latest_status:
        columns += ("Latest status",)
    lines = ["| " + " | ".join(columns) + " |", "| " + " | ".join("---" for _ in columns) + " |"]
    for item in visible:
        owner = record.by_id(item.owner) if not isinstance(item.owner, UnassignedOwner) else None
        state = item.state.value
        if item.state is ItemState.SCHEDULED and isinstance(item.schedule, ScheduledRun):
            state += " · Starts " + _schedule_time_text(item.schedule.start)
        elif item.state is ItemState.SCHEDULED and isinstance(item.schedule, ScheduledSeries):
            state += " · Starts " + _schedule_time_text(item.schedule.first.start)
            state += " · " + item.schedule.describe()
            next_run = item.schedule.next_run()
            state += " · next " + _schedule_time_text(next_run.run.start) if isinstance(next_run, SeriesRun) else " · no runs remain"
        if item.state != ItemState.CLOSED and isinstance(item.next_check, datetime):
            state += "; " + ("check overdue " if item.next_check < moment else "check ") + item.next_check.isoformat()
        if item.state != ItemState.CLOSED and isinstance(item.next_check, CheckCondition):
            state += "; check: " + item.next_check.text
        if item.parked_until is ParkUntilDelivered.DELIVERED:
            state += "; parked until delivered"
        elif isinstance(item.parked_until, datetime) and item.parked_until > moment:
            state += "; parked until " + parking_date(item.parked_until, user_zone())
        producer = record.by_id(item.producer)
        cells = (str(item.number), owner.name if owner is not None else item.owner,
                 producer.name if producer is not None else item.producer, item.title, state,
                 item.next_step, eta_text(item.eta), "●" if isinstance(item.unread_since, datetime) else "—")
        if show_latest_status:
            status = item.latest_status
            status_time = item.latest_status_time
            if status and isinstance(status_time, datetime):
                status += " · " + list_time(status_time, today)
            cells += (status,)
        lines.append("| " + " | ".join(" ".join(cell.split()).replace("|", "\\|") for cell in cells) + " |")
    return "\n".join(lines)


def producer_id(members: staff.StaffSnapshot, reference: str) -> staff.StaffId:
    """Resolve a producer name or fixed staff id; refuse every other staff row."""
    matches = [row for row in staff.active(members, "producer") if reference in (row.id, row.name)]
    if len(matches) != 1:
        raise ListError("producer must name one producer staff row by name or staff id")
    return matches[0].id


def _check_cli_text_limits(changes: ItemChanges, add_evidence: str | None) -> None:
    lengths: list[tuple[str, str, int]] = []
    for field, value in (("latest_status", changes.get("latest_status")), ("next_step", changes.get("next_step"))):
        if value is not None:
            lengths.append((f"max_chars.{field}", "--" + field.replace("_", "-"), len(value)))
    if "evidence" in changes:
        longest = max(map(len, changes["evidence"].splitlines()), default=0)
        lengths.append(("max_chars.evidence_line", "--evidence", longest))
    if add_evidence is not None:
        lengths.append(("max_chars.evidence_line", "--add-evidence", len(add_evidence)))
    if not lengths:
        return
    settings = conf.read("the_list")
    for key, option, length in lengths:
        limit = settings.origin(key, conf.positive_whole)
        if not isinstance(limit, conf.UsableLiveValue):
            raise ListError(f"{option} needs a valid {key} in {settings.source}")
        if length > limit.value:
            unit = "characters per line" if key == "max_chars.evidence_line" else "characters"
            raise ListError(f"{option} limit is {limit.value} {unit}. write it for nate: two short sentences")


def _cli_state(value: str) -> str:
    return ItemState.SCHEDULED.value if value == "scheduled" else value


def _cli_changes(args: argparse.Namespace, members: staff.StaffSnapshot, add_evidence: str | None) -> ItemChanges:
    options = cast(dict[str, object], vars(args))
    changes: ItemChanges = {}
    text_fields: tuple[Literal["title", "unit", "next_step", "latest_status", "evidence", "done_when"], ...] = (
        "title", "unit", "next_step", "latest_status", "evidence", "done_when")
    for key in text_fields:
        value = options.get(key)
        if isinstance(value, str):
            changes[key] = value
    owner_id = options.get("owner")
    if isinstance(owner_id, str):
        if owner_id in UnassignedOwner:
            changes["owner"] = UnassignedOwner(owner_id)
        else:
            owner = members.by_id(staff.StaffId(owner_id))
            if owner is None or owner.kind != "showrunner":
                raise ListError("owner must name a showrunner staff id")
            changes["owner"] = owner.id
    producer = options.get("producer")
    if isinstance(producer, str):
        changes["producer"] = producer_id(members, producer)
    state, next_check, eta, requested = (options.get(key) for key in ("state", "next_check", "eta", "requested_by"))
    if isinstance(state, str):
        changes["state"] = ItemState(state)
    if isinstance(next_check, str):
        changes["next_check"] = next_check_value(next_check)
    if isinstance(eta, str):
        changes["eta"] = _cli_eta(eta)
    if isinstance(requested, str):
        changes["requested_by"] = RequestedBy(requested)
    blocked_by, related_to = options.get("blocked_by"), options.get("related_to")
    if isinstance(blocked_by, list):
        dependencies: list[ItemDependency] = []
        for text in cast(list[str], blocked_by):
            number, condition = text.split(":", 1)
            dependencies.append(ItemDependency(int(number.removeprefix("L").removeprefix("#")), condition))
        changes["blocked_by"] = tuple(dependencies)
    if isinstance(related_to, list):
        changes["related_to"] = tuple(int(text.removeprefix("L").removeprefix("#")) for text in cast(list[str], related_to))
    _check_cli_text_limits(changes, add_evidence)
    descriptions = (value.text for value in (changes.get("eta"), changes.get("next_check"))
                    if isinstance(value, (EtaDescription, CheckCondition)))
    console_text.require((changes.get("title", ""), changes.get("next_step", ""), changes.get("latest_status", ""),
                          changes.get("evidence", ""), changes.get("done_when", ""), add_evidence or "",
                          *descriptions, *(dependency.condition for dependency in changes.get("blocked_by", ()))),
                         "list field", refusal=ListError)
    return changes


def _cli_schedule_edits(args: argparse.Namespace) -> ScheduleChanges:
    options = cast(dict[str, object], vars(args))
    edits: ScheduleChanges = {}
    start, words, rule = options.get("start"), options.get("do"), options.get("repeat")
    if isinstance(start, str):
        edits["start"] = _scheduled_time(start)
    if isinstance(words, str):
        edits["words"] = words
    if isinstance(rule, str):
        edits["repeat"] = repeat_rule.parse(rule) if rule else RemoveRepeat.KEEP_ONE_RUN
    return edits

def _cli_item_id(text: str) -> str:
    """Accept an item number with or without its L prefix."""
    return f"L{text}" if text.isascii() and text.isdecimal() else text


def _send_assignment_notice(recipient: staff.StaffRow, text: str) -> None:
    command = session_send.send_argv("--to", recipient.id, "--recipient-kind", recipient.kind,
                                     "--from", "morty", "--text", text)
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if send_result.delivered_or_kept(result.returncode, result.stdout, accept_skipped=True):
        return
    raise ListError((result.stderr or result.stdout).strip() or "message delivery failed")


def notify_producer_assignment(item: ListItem, previous_producer: staff.StaffId, keeper: staff.StaffId, *,
                               caller: staff.StaffId) -> int:
    """Send the notices for a saved producer assignment."""
    if item.producer in (keeper, previous_producer):
        return 0
    notify_producer = item.producer != caller
    try:
        members = staff.snapshot()
    except (ValueError, staff.StaffRefused, OSError) as error:
        unsent = "The producer and showrunner notices were" if notify_producer else "The showrunner notice was"
        raise ListError(f"{item.id} was saved. {unsent} not sent. {error}") from error
    producer = next((row for row in staff.active(members, "producer") if row.id == item.producer), None)
    owner = next((row for row in staff.active(members, "showrunner") if row.id == item.owner), None)
    producer_owner = any(row.id == item.owner for row in staff.active(members, "producer"))
    showrunner = f"Its showrunner is {owner.name}." if owner is not None else "The item has no assigned showrunner."
    item_text = f"item {item.number}: {item.title}"
    options = PRODUCER_SCHEDULE_UPDATE_OPTIONS if item.state is ItemState.SCHEDULED else _PRODUCER_UPDATE_OPTIONS
    update_options = ", ".join(f"`{option}`" for option in options)
    notices = (
        ("producer", item.producer, producer, f"You are the producer of {item_text}. {showrunner}\n"
         + f"Run `{_LAUNCHER} list --all --latest-status` to read it. "
         + f"Run `{_LAUNCHER} update --id {item.id}` to update it. Use any of {update_options}."),
        ("showrunner", item.owner, owner, f"The saved assignment for {item_text} needs a new producer."
         if producer is None else f"{producer.name} is the producer of {item_text}. "
         + f"Send design questions on this item to {producer.name}."),
    )
    status = 0
    for role, reference, recipient, message in notices:
        if role == "producer" and not notify_producer:
            continue
        if role == "showrunner" and producer_owner:
            continue
        try:
            if recipient is None:
                if role == "producer":
                    raise ListError("The producer has no current producer staff row.")
                if isinstance(reference, UnassignedOwner):
                    raise ListError("the item has no assigned showrunner")
                raise ListError("the owner has no current showrunner staff row")
            _send_assignment_notice(recipient, message)
        except (ListError, OSError, subprocess.SubprocessError, UnicodeError) as error:
            details = " ".join(str(error).splitlines())
            print(f"the_list: {item.id} was saved. The {role} notice to {reference} failed. {details}", file=sys.stderr)
            status = 1
    return status


class _GoalArguments(argparse.Namespace):
    action: str = ""
    item: str = ""
    blocked: str = ""
    faster: str = ""
    keeper: staff.StaffId | None = None


def _set_goal(producer: staff.StaffId, identity: str, *, keeper: staff.StaffId) -> str:
    with _store(keeper).locked(), goal.locked(producer):
        saved = goal.load(producer)
        item = load(keeper, identity)
        if item.producer != producer:
            raise ListError(f"{item.id} is assigned to another producer.")
        if saved is goal.NoGoal.UNSET or (saved["keeper"], saved["item"]) != (keeper, item.id):
            saved = goal.GoalRecord(keeper=keeper, item=item.id, blocked="", faster="", noted_at="")
        goal.write_goal(producer, saved)
        return goal.save(producer, saved, _goal_status(item))


def _refresh_goal(producer: staff.StaffId, *, note: tuple[str, str] | None = None) -> str:
    with goal.locked(producer):
        saved = goal.load(producer)
        if saved is goal.NoGoal.UNSET:
            if note is not None:
                raise ListError(saved.value)
            goal.clear_card(producer)
            return saved.value
        item = load(saved["keeper"], saved["item"])
        if item.producer != producer:
            if note is not None:
                raise ListError(f"{item.id} is assigned to another producer.")
            return goal.save(producer, saved, goal.ItemAvailability.UNAVAILABLE)
        if note is not None:
            blocked, faster = note
            _line(blocked, "Blocked by")
            _line(faster, "Faster if")
            saved["blocked"] = blocked.strip()
            saved["faster"] = faster.strip()
            saved["noted_at"] = now().isoformat(timespec="seconds")
            goal.write_goal(producer, saved)
        return goal.save(producer, saved, _goal_status(item))


def goal_main(argv: list[str] | None = None) -> int:
    """Run the producer's main goal command."""
    parser = argparse.ArgumentParser(description=goal.__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    mark = actions.add_parser("set", help="mark one List item as your main goal")
    _ = mark.add_argument("item", help="the List item ID, such as L1")
    _ = mark.add_argument("--keeper", type=staff.StaffId, help=_KEEPER_HELP + _CALLER_FIRST_HELP)
    _ = actions.add_parser("card", help="refresh and print your main goal card")
    note = actions.add_parser("note", help="write the blocker and what can make work faster")
    _ = note.add_argument("--blocked", required=True, help="what blocks the main goal")
    _ = note.add_argument("--faster", required=True, help="what can make work faster")
    args = parser.parse_args(argv, namespace=_GoalArguments())
    try:
        members = staff.snapshot()
        producer = members.by_current_session(staff.caller_identity())
        if producer is None or producer not in staff.active(members, "producer"):
            raise ListError("Run this command in a producer session.")
        if args.action == "set":
            keeper = _record_keeper(_active_morty(members), producer.id, args.item, args.keeper,
                                    policy=_KeeperRouting.CALLER_FIRST)
            card = _set_goal(producer.id, args.item, keeper=keeper)
        elif args.action == "note":
            card = _refresh_goal(producer.id, note=(args.blocked, args.faster))
        else:
            card = _refresh_goal(producer.id)
        print(card)
    except (OSError, ValueError, staff.StaffRefused) as error:
        print(f"goal: {error}", file=sys.stderr)
        return 1
    return 0


_KEEPER_HELP = "the staff ID of the record keeper"
_CALLER_FIRST_HELP = " (defaults to your folder if it holds the item, else morty's)"


class _KeeperRouting(StrEnum):
    CALLER_FIRST = "caller-first"
    UPDATE = "update"


def _active_morty(members: staff.StaffSnapshot) -> staff.StaffId | _NoActiveMorty:
    try:
        return producer_id(members, "morty")
    except ListError:
        return _NoActiveMorty.UNAVAILABLE


def _record_keeper(morty: staff.StaffId | _NoActiveMorty, caller: staff.StaffId, identity: str,
                   chosen: staff.StaffId | None, *, policy: _KeeperRouting) -> staff.StaffId:
    """Select the List folder for the command.

    The locked write checks the caller against the saved record.
    """
    if chosen is not None:
        if not _store(chosen).folder().is_dir():
            raise ListError(f"no List folder for keeper {chosen}")
        return chosen
    if policy is _KeeperRouting.CALLER_FIRST:
        own = _store(caller)
        if own.valid_id(identity) and os.path.lexists(own.path(identity)):
            return caller
    if morty is _NoActiveMorty.UNAVAILABLE:
        raise ListError("default List routing requires one active morty staff row")
    return morty


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    confirm = commands.add_parser("confirm", help="confirm the current scheduled run")
    _ = confirm.add_argument("--id", required=True)
    _ = confirm.add_argument("--run", default="", help="the run ID in the start message")
    answer = commands.add_parser("answer", help="save an answer to a List question")
    _ = answer.add_argument("id", help="the List item ID, such as L1")
    _ = answer.add_argument("--question", required=True, help="the question ID, such as Q1")
    _ = answer.add_argument("--text", required=True, help="the answer to show on The List")
    _ = answer.add_argument("--keeper", type=staff.StaffId, help=_KEEPER_HELP + _CALLER_FIRST_HELP)
    ask = commands.add_parser("ask", help="ask nate for a Go or an approval on the List page")
    _ = ask.add_argument("id", help="the List item ID, such as L1")
    _ = ask.add_argument("--kind", required=True, choices=[kind.value for kind in AskKind])
    _ = ask.add_argument("--target", required=True, help="the staff ID of the session that needs the Go")
    _ = ask.add_argument("--what", required=True, help="the one change that nate approves")
    _ = ask.add_argument("--why", required=True, help="why the session needs nate's word")
    _ = ask.add_argument("--config", action="store_true", help="the change is a settings.json, CLAUDE.md or config change")
    _ = commands.add_parser("move-review-bugs", help="move open Review bugs to the List")
    _ = commands.add_parser("move-review-waits", help="move Waiting and Validating Review records to the List")
    withdrawal = commands.add_parser("withdraw-ask", help="withdraw an open List ask")
    _ = withdrawal.add_argument("id", help="the List item ID, such as L1")
    _ = withdrawal.add_argument("--ask", required=True, help="the ask ID, such as A1")
    _ = withdrawal.add_argument("--reason", required=True, help="why the ask is no longer needed")
    top = commands.add_parser("top-priority", help="make an open item the top priority item of its producer")
    _ = top.add_argument("--id", required=True, help="the List item ID, such as L1")
    for name in ("add", "update", "park", "unpark", "close", "reopen", "mark-read", "clean-evidence", "link", "unlink", "list"):
        command = commands.add_parser(name)
        if name != "list":
            _ = command.add_argument("--source", required=name != "update")
        if name in ("link", "unlink"):
            _ = command.add_argument("id")
            _ = command.add_argument("--review", dest="reviews", action="append", type=ReviewByShownId, default=[])
            _ = command.add_argument("--review-key", dest="reviews", action="append", type=ReviewByKey, default=[])
        elif name not in ("add", "list", "clean-evidence"):
            _ = command.add_argument("--id", required=True)
        if name == "update":
            _ = command.add_argument("--keeper", type=staff.StaffId, help=_KEEPER_HELP + " (defaults to morty's)")
        if name in ("add", "update"):
            _ = command.add_argument("--title", required=name == "add")
            _ = command.add_argument("--owner", required=name == "add")
            _ = command.add_argument("--producer")
            for key in ("unit", "next-check", "eta", "next-step", "latest-status", "evidence", "add-evidence", "done-when"):
                _ = command.add_argument("--" + key)
            _ = command.add_argument("--state", type=_cli_state,
                                     choices=[state.value for state in ItemState if state != ItemState.CLOSED])
            _ = command.add_argument("--start", help="ISO time with an offset or [IANA zone]")
            _ = command.add_argument("--do", help="At that time, do this")
            _ = command.add_argument("--repeat", help="RFC 5545 rule, or empty to remove the repeat")
            _ = command.add_argument("--requested-by", choices=list(RequestedBy))
            _ = command.add_argument("--blocked-by", nargs="*")
            _ = command.add_argument("--related-to", nargs="*")
        if name in ("update", "park", "close"):
            _ = command.add_argument("--scope", type=ScheduleScope,
                                     choices=(ScheduleScope.THIS, ScheduleScope.FOLLOWING, ScheduleScope.ALL),
                                     default=ScheduleScope.UNSPECIFIED)
        if name == "add":
            _ = command.add_argument("--number", type=int)
        elif name == "park":
            _ = command.add_argument("--until", required=True, help="a date, a time with a UTC offset, or delivered")
        elif name == "close":
            _ = command.add_argument("--words", required=True)
        elif name == "list":
            _ = command.add_argument("--all", action="store_true")
            _ = command.add_argument("--include-parked", action="store_true")
            _ = command.add_argument("--latest-status", action="store_true", help="show the latest status column")
    args = parser.parse_args(argv)
    name = cast(str, args.command)
    if name in ("link", "unlink") and not cast(list[ReviewByShownId | ReviewByKey], args.reviews):
        parser.error("use --review or --review-key")
    try:
        source = cast(str | None, getattr(args, "source", None))
        if source is not None and source_without_clock(source) != source:
            raise ListError("Omit the clock time from --source because the tool adds the time.")
        add_evidence = cast(str | None, getattr(args, "add_evidence", None))
        if add_evidence is not None:
            _line(add_evidence, "evidence to add")
        if name == "confirm" and sys.platform == "darwin":
            return _confirm_on_natedev(cast(str, args.id), cast(str, args.run))
        record = staff.snapshot()
        session = staff.runtime_id() if name == "withdraw-ask" else staff.caller_identity()
        caller = record.by_current_session(session) if session or name != "withdraw-ask" else None
        if caller is None:
            raise ListError("the current session has no staff row")
        if name == "confirm":
            keeper = producer_id(record, "morty")
            identity = cast(str, args.id)
            confirm_scheduled(keeper, identity, caller, run_key=cast(str, args.run))
            print(f"confirmed {identity}")
            return 0
        if caller.kind != "producer" and name != "close":
            raise ListError("the current session must be a producer")
        if name == "answer":
            identity, question = cast(str, args.id), cast(str, args.question)
            answer_morty = _active_morty(record)
            keeper = _record_keeper(answer_morty, caller.id, identity, cast(staff.StaffId | None, args.keeper),
                                    policy=_KeeperRouting.CALLER_FIRST)
            answerer = _QuestionAnswerer(caller, answer_morty)
            result = page_question.answer(_ListQuestionStore(keeper, answerer=answerer), identity, question,
                                          cast(str, args.text))
            if isinstance(result, page_question.AnswerRefused):
                raise ListError(result.reason)
            print(f"answered {identity} {question}")
            return 0
        morty = producer_id(record, "morty")
        list_keeper = record_store.list_keeper(conf.read_or_repository("console"), record)
        if list_keeper is None:
            raise ListError("The List keeper is unavailable.")
        keeper = list_keeper.id
        if name in ("ask", "withdraw-ask"):
            identity = cast(str, args.id)
            identity = f"L{identity}" if identity.isascii() and identity.isdecimal() else identity
            if name == "withdraw-ask":
                ask_id = cast(str, args.ask)
                withdraw_ask(keeper, identity, ask_id, cast(str, args.reason), caller=caller)
                print(f"withdrew {identity} {ask_id}")
                return 0
            target = staff.StaffId(cast(str, args.target))
            if record.by_id(target) is None:
                raise ListError("--target must name a staff id")
            what, why = cast(str, args.what), cast(str, args.why)
            console_text.require((what, why), "list field", refusal=ListError)
            opened = add_ask(keeper, identity, kind=AskKind(cast(str, args.kind)), target=target,
                             what=what, why=why, config=cast(bool, args.config),
                             requester=caller.id, source=caller.name)
            print(f"{identity} {opened.ask_id}")
            return 0
        # Only morty updates every field of every item, in each keeper's folder.
        assigned_to = AllItems.INCLUDED if caller.id == morty else caller.id
        if name in ("move-review-bugs", "move-review-waits"):
            if caller.id != morty:
                raise ListError(f"only morty can run {name}")
            if name == "move-review-bugs":
                destinations = move_review_bugs(record)
            else:
                from scripts.review_links import review_link

                destinations = move_review_waits(review_link.review_root(), record)
            for destination in destinations:
                print(f"moved to List item {destination.number}")
            return 0
        if name == "list":
            include_closed = cast(bool, args.all)
            if assigned_to is AllItems.INCLUDED:
                items = records(keeper, include_closed=include_closed)
                incomplete = False
            else:
                read = _producer_records(keeper, assigned_to, include_closed=include_closed)
                items = read.items
                incomplete = read.incomplete
            print(_format_table(items, record, include_parked=include_closed or cast(bool, args.include_parked),
                                show_latest_status=cast(bool, args.latest_status)))
            if incomplete:
                raise ListError("The List results are incomplete. Could not read every record.")
            return 0
        schedule_scope = cast(ScheduleScope, getattr(args, "scope", ScheduleScope.UNSPECIFIED))
        if name == "update":
            identity = _cli_item_id(cast(str, args.id))
            keeper = _record_keeper(list_keeper.id, caller.id, identity, cast(staff.StaffId | None, args.keeper),
                                    policy=_KeeperRouting.UPDATE)
            reassigns = cast(str | None, args.producer) is not None or cast(str | None, args.owner) is not None
            # A producer gets the field refusal below, because it never changes these fields.
            if assigned_to is AllItems.INCLUDED and keeper != list_keeper.id and reassigns:
                raise ListError("move the item to morty's List before you change its producer or owner")
            if assigned_to is not AllItems.INCLUDED:
                options = cast(dict[str, object], vars(args))
                _check_producer_update_fields(key for key, value in options.items()
                                              if value is not None and key not in {"command", "id", "keeper", "source"}
                                              and value is not ScheduleScope.UNSPECIFIED)
                _ = _update(keeper, identity, _cli_changes(args, record, add_evidence), source=source or "",
                            add_evidence=add_evidence or "", assigned_to=caller,
                            schedule_edits=_cli_schedule_edits(args), schedule_scope=schedule_scope, say_schedule=True)
                return 0
            if source is None:
                parser.error("update requires --source for morty")
            changed = _update(keeper, identity, _cli_changes(args, record, add_evidence), source=source,
                              add_evidence=add_evidence or "", schedule_edits=_cli_schedule_edits(args),
                              schedule_scope=schedule_scope, say_schedule=True)
            if isinstance(changed, UnchangedEdit):
                return 0
            return notify_producer_assignment(changed.item, changed.before.producer, keeper, caller=caller.id)
        if name == "top-priority":
            item = make_top_priority(keeper, _cli_item_id(cast(str, args.id)), assigned_to=assigned_to)
            producer = record.by_id(item.producer)
            print(f"{item.id} is the top priority item of {producer.name if producer is not None else item.producer}")
            return 0
        if assigned_to is not AllItems.INCLUDED and name not in ("add", "park", "unpark", "close", "reopen"):
            raise ListError(f"only morty can run {name}")
        assert source is not None
        if name == "clean-evidence":
            changed, unchanged = clean_evidence(keeper, source=source)
            print(f"{changed} changed; {unchanged} unchanged")
        elif name in ("link", "unlink"):
            identity = cast(str, args.id)
            selections = cast(list[ReviewByShownId | ReviewByKey], args.reviews)
            current = load(keeper, identity)
            _open(current)
            from scripts.review_links import review_items, review_link

            needs_read = any(isinstance(selection, ReviewByShownId)
                             or (name == "link" and selection.value not in current.reviews) for selection in selections)
            known = ({item.key: item for item in review_items.read(review_link.review_root(), members=record)}
                     if needs_read else {})
            resolved: list[str] = []
            for selection in selections:
                if isinstance(selection, ReviewByKey):
                    if name == "link" and selection.value not in current.reviews and selection.value not in known:
                        raise ListError("unknown review key: " + selection.value)
                    resolved.append(selection.value)
                    continue
                matches = [item for item in known.values() if item.id == selection.value]
                if len(matches) > 1:
                    choices = ", ".join(f"{item.key} ({item.type.value})" for item in matches)
                    raise ListError(f"ambiguous review id: {selection.value}; {choices}")
                if not matches and name == "link":
                    raise ListError("unknown review id: " + selection.value)
                resolved.extend(item.key for item in matches)
            edit_reviews = link if name == "link" else unlink
            _ = edit_reviews(keeper, identity, resolved, source=source)
        elif name == "add":
            changes = _cli_changes(args, record, add_evidence)
            number = cast(dict[str, object], vars(args)).get("number")
            if assigned_to is not AllItems.INCLUDED:
                if changes.get("producer", caller.id) != caller.id:
                    raise ListError(f"To add an item, use --producer {caller.name} or your staff ID.")
                if number is not None:
                    raise ListError("only morty can choose an item number")
                changes["producer"] = caller.id
                source = caller.name
            if add_evidence is not None:
                changes["evidence"] = _append_evidence(changes.get("evidence", ""), add_evidence)
            owner = changes.get("owner")
            if owner is None:
                raise ListError("owner must name a showrunner staff id")
            schedule_edits = _cli_schedule_edits(args)
            if schedule_edits or changes.get("state") is ItemState.SCHEDULED:
                if changes.get("state") is not ItemState.SCHEDULED:
                    raise ListError("use --state Scheduled with schedule fields")
                schedule = _schedule_edit(NoSchedule.UNSET, schedule_edits, ScheduleScope.UNSPECIFIED, now(),
                                          repeat_zone=RepeatInSelectedRunZone.KEEP)
                _validate_schedule(schedule)
                _check_series_limit(schedule)
                changes["schedule"] = schedule
                _say_schedule(schedule)
            item = add(keeper, cast(str, args.title), owner=owner, source=source,
                       number=number if isinstance(number, int) else NextNumber.AVAILABLE, changes=changes,
                       assigned_to=assigned_to)
            print(item.id)
            return notify_producer_assignment(item, keeper, keeper, caller=caller.id)
        elif name == "park":
            _ = park(keeper, cast(str, args.id), parking_value(cast(str, args.until)), source=source,
                     assigned_to=assigned_to, schedule_scope=schedule_scope)
        elif name == "unpark":
            _ = unpark(keeper, cast(str, args.id), source=source, assigned_to=assigned_to)
        elif name == "close":
            words = cast(str, args.words)
            console_text.require(words, "list field", refusal=ListError)
            close_access = AllItems.INCLUDED if assigned_to is AllItems.INCLUDED else ListCloseCaller(caller)
            _ = close(keeper, cast(str, args.id), words, source=source if caller.kind == "producer" else caller.name,
                      assigned_to=close_access, schedule_scope=schedule_scope)
        elif name == "reopen":
            _ = reopen(keeper, cast(str, args.id), source=source, assigned_to=assigned_to)
        else:
            _ = mark_read(keeper, cast(str, args.id), source=source)
        return 0
    except (ValueError, staff.StaffRefused, OSError, UnicodeError) as error:
        print(f"the_list: {str(error).replace(chr(10), ' ')}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

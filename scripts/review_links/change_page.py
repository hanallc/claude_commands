#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Publish all change reviews, or retain an older selected-row link with --production NAME C27."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import fcntl
import hashlib
import html
import json
import math
import os
import re
import secrets
import shlex
import socket
import subprocess
import sys
import threading
import time
import unicodedata
from collections.abc import Callable, Generator, Iterable
from contextlib import AbstractContextManager, ExitStack, contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from functools import partial
from importlib import import_module
from pathlib import Path
from typing import IO, Literal, NotRequired, TypedDict, cast, overload
from urllib.parse import parse_qs, urlsplit
from weakref import WeakSet, WeakValueDictionary, finalize

from scripts.agents import remote
from scripts.lib import build_folders, process_start
from scripts.lib.boot_identity import boot_identity
from scripts.message import send_budget, send_result
from scripts.production import change_review as change_review
from scripts.production import investigation, proposal_issue, unit_lookup, record_store
from scripts.production.issues import Issue, source_paths
from scripts.production import update_registration
from scripts.production import unrouted_alert, change_routing, delivery_state, next_item, showrunners, staff
from scripts.production.delivery_state import (
    NoQuestionSendOwner, NoQuestionSendReceipt, QuestionAttempt, QuestionAttemptMissing as QuestionAttemptMissing,
    QuestionSendOwner, QuestionSendReceipt, QuestionSendState, QuestionSource,
    AnsweredQuestion as AnsweredQuestion, DeliveryIntent as DeliveryIntent, DeliverySlots as DeliverySlots,
    NoDelivery as NoDelivery, NoQuestion as NoQuestion, OpenQuestion as OpenQuestion,
    ValidationRequested as ValidationRequested,
)
from scripts.lib import atomic_file, conf, file_lock, urgency
from scripts.lib.user_zone import user_zone
from scripts.production.production_doc import (
    Refusal as Refusal, production_field, read_production,
)
from scripts.feature_log import feature_log
from scripts.review_links import (console_theme, image_store, new_item_dialog, page_frame, question_record, review_events,
                                  review_link)
from scripts.review_links.question_record import AnswerRefused as AnswerRefused
from scripts.review_links.review_items import (
    CompletedFeedback as CompletedFeedback, OpenFeedback as OpenFeedback,
    decode_change_delivery, read_change_delivery, validation_recipients,
)

MANIFEST = ".review-link-changes.json"
PAGE_NAME = "changes.html"
REBUILDS_NAME = "rebuilds.html"
BROWSER_MANIFEST = ".review-link-browser.json"
CONSOLE_FOLDER = "console"
SEND_ERRORS = (OSError, RuntimeError, subprocess.SubprocessError)
SENDER = Path(__file__).stem
DELIVERY_PREFIX = ".review-link-delivery-"
CLOSEOUT_PREFIX = ".review-link-closeout-alert-"
QUESTION_RECOVERY_PREFIX = ".review-link-question-recovery-"
IMAGE_QUESTION_ROW_PREFIX = "Images "
QUESTION_RECOVERY_DEFAULT_SECONDS = 1800
QUESTION_RECOVERY_EXPIRED = "The question delivery deadline expired."
QUESTION_NOT_STARTED = "Question send not started."


class BrowserManifest(TypedDict):
    token: str
    initial: str


type ReviewKind = Literal["changes", "questions"]
TITLES: dict[ReviewKind, str] = {"changes": "Changes to review", "questions": "Questions to answer"}


@dataclass(frozen=True)
class ReviewBrowser:
    token: str
    initial: str = ""
    kind: ReviewKind = "changes"


class PageManifest(TypedDict):
    review_list: str
    production: str
    rows: list[str]
    token: str


class StoredDeliveryIntent(TypedDict):
    attempt: str
    state: Literal["pending"]
    recipients: NotRequired[list[str]]


class StoredValidationIntent(StoredDeliveryIntent):
    feedback: str


class StoredValidationRequested(TypedDict):
    attempt: str
    state: Literal["sent"]
    feedback: str
    recipients: list[str]


class StoredOpenQuestion(TypedDict):
    question_id: str
    text: str
    delivery: StoredDeliveryIntent | None
    kind: NotRequired[Literal["feedback"]]


class StoredAnsweredQuestion(TypedDict):
    question_id: str
    text: str
    answer: str
    kind: NotRequired[Literal["feedback"]]


class StoredDeliverySlots(TypedDict, total=False):
    handed_at: str
    waiting_why: str
    validation: StoredValidationIntent | StoredValidationRequested
    verdict: StoredDeliveryIntent
    question: StoredOpenQuestion | StoredAnsweredQuestion
    user_answer: StoredUserAnswer
    review_message: StoredReviewMessage
    question_delivery: StoredQuestionProgress
    change_delivery: dict[str, object]


WITH_SHOWRUNNER = "This change is already with the showrunner."
CHANGE_HANDOFFS = frozenset(("validate", "question", "feedback"))
VALIDATION_QUESTION = "validation-question"


def read_handed_at(path: Path) -> str | None:
    """Read the saved hand-off field without changing its value."""
    try:
        fields = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (FileNotFoundError, ValueError):
        return None
    saved = cast(dict[str, object], fields).get("handed_at") if isinstance(fields, dict) else None
    return saved if isinstance(saved, str) else None


def require_user_turn(slots: DeliverySlots) -> None:
    if not slots.waits_for_user(False):
        raise ChangeHandoffRefused(WITH_SHOWRUNNER)


class StoredReviewMessage(TypedDict):
    saved: bool
    source: str
    owner: str
    message: str
    # A record saved before efficiencies has no row; its row is the source's stem.
    row: NotRequired[str]
    submission_request: NotRequired[str]
    submission_delivered: NotRequired[bool]
    question_save_uncertain: NotRequired[Literal[True]]
    question_source_key: NotRequired[str]


class StoredDeliveryCloseout(TypedDict):
    review_list: str
    row: str
    messages: list[str]
    failed_delivery: NotRequired[StoredFailedDelivery]
    review_dispatch: NotRequired[StoredReviewDispatch]
    question_dispatch: NotRequired[StoredQuestionDispatch]
    pending_alerts: NotRequired[list[PendingDeliveryAlert]]


class PendingDeliveryAlert(TypedDict):
    recipient: staff.StaffId
    message: str
    key: NotRequired[str]
    state: NotRequired[QuestionDeliveryState]
    deadline: NotRequired[float]


class StoredReviewDispatch(TypedDict):
    attempt: str
    state: Literal["started", "finished"]
    keyed: NotRequired[Literal[True]]


class StoredQuestionDispatch(TypedDict):
    attempt: str
    state: Literal["started", "finished", "failed", "unknown"]
    question_id: NotRequired[str]
    error: NotRequired[str]
    recipients: NotRequired[dict[staff.StaffId, Literal["sent", "unknown"]]]
    sending_to: NotRequired[staff.StaffId]


class StoredFailedDelivery(TypedDict):
    attempt: str
    review_list: str
    row: str
    last_error: str


@dataclass(frozen=True)
class FailedDelivery:
    """The last attempt failed after all retries. A later success clears this result."""

    attempt: str
    review_list: Path
    row: str
    last_error: str


@dataclass(frozen=True)
class NoFailedDelivery:
    """This row has no saved delivery failure."""


NO_FAILED_DELIVERY = NoFailedDelivery()


QuestionOutcome = Literal["answer", "followup", "skip", "question", "feedback"]


class StoredUserAnswer(TypedDict):
    saved: bool
    source: str
    production: str
    owner: str
    question: str
    answer: str
    message: str
    # A record saved before follow-ups and skips has no kind; it is an answer.
    kind: NotRequired[QuestionOutcome]
    question_id: NotRequired[str]
    question_text: NotRequired[str]
    question_source_key: NotRequired[str]
    legacy_followup: NotRequired[Literal[True]]


@dataclass(frozen=True)
class QuestionDelivery:
    """One question and its message attempt share the same saved identity."""

    question_id: str
    text: str
    attempt: str


@dataclass(frozen=True)
class ReviewPage:
    review_list: Path
    production: Path
    rows: tuple[str, ...]
    token: str


@dataclass(frozen=True)
class AssignedReviewPage(ReviewPage):
    """The staff record supplies this row's current showrunner."""

    owner: change_routing.ChangeOwner


@dataclass(frozen=True)
class UserQuestionPage(ReviewPage):
    """A filed user question and its saved answer share one delivery record."""

    saved: StoredUserAnswer

    def __post_init__(self) -> None:
        if self.saved.get('kind') == 'question':
            if 'question_id' not in self.saved:
                self.saved['question_id'] = secrets.token_hex(4)
            identity = self.saved['question_id']
            key = 'question/'+self.saved['owner']+'/'+Path(self.saved['source']).name
            request = self.saved['message'].split('\nAnswer with this command:\n', 1)[0].partition('\n')[2]
            if request.startswith('For: '):
                request = request.partition('\n')[2]
            _ = self.saved.setdefault('question_text', request)
            self.saved['message'] = self.saved['message'].replace(reply_command(key), reply_command(key, identity))


@dataclass(frozen=True)
class ReviewMessagePage(ReviewPage):
    """A Review record and its saved message use the existing delivery slots."""

    saved: StoredReviewMessage


class QuestionAnswerRefused(question_record.AnswerRefused):
    """The browser can show this refusal to the user."""


class ChangeHandoffRefused(QuestionAnswerRefused):
    """A showrunner already owns the next action for the change."""


class FeatureLogFailed(RuntimeError):
    """The feature log failed before the verdict could save."""


@dataclass(frozen=True)
class AnswerNotice:
    row: str = ""
    reason: str = ""
    outcome: str = ""
    feedback: str = ""


NO_ANSWER_NOTICE = AnswerNotice()


@dataclass(frozen=True)
class MessageDelivery:
    page: ReviewPage
    row: str
    recipients: tuple[staff.StaffId, ...]
    message: str
    attempt: str
    alert_recipients: tuple[staff.StaffId, ...] = ()
    scratch_cleanup: change_review.PendingVerdictCleanup | change_review.NoVerdictCleanup = change_review.NoVerdictCleanup()

    def __post_init__(self) -> None:
        if isinstance(self.page, UserQuestionPage) and self.page.saved.get('kind') == 'question':
            object.__setattr__(self, 'message', self.page.saved['message'])


@dataclass(frozen=True)
class NoMessageDelivery:
    """This answer needs no message."""
    scratch_cleanup: change_review.PendingVerdictCleanup | change_review.NoVerdictCleanup = change_review.NoVerdictCleanup()


@dataclass(frozen=True)
class NoQuestionJob:
    """This submission does not own a delivery job."""


NO_QUESTION_JOB = NoQuestionJob()


@dataclass(frozen=True)
class SavedQuestionDelivery:
    source: QuestionSource
    recipient: staff.StaffId
    title: str
    answer_command: str

    @property
    def heading(self) -> str:
        return f"{self.source.row.rsplit('/', 1)[0]}: Ask question"

    @property
    def message_body(self) -> str:
        return f"{self.source.words}\nAnswer with this command: {self.answer_command}"

    @property
    def message(self) -> str:
        return f"{self.heading}\n{self.title}\n{self.message_body}"

    def page(self) -> ReviewPage:
        path = Path(self.source.path)
        return ReviewPage(path, path.parent, (self.source.row,), "")

    def matches(self, record: QuestionAttempt) -> bool:
        return (record.source == self.source and record.recipient_id == self.recipient
                and self.matches_message(record.message))

    def matches_message(self, message: str) -> bool:
        if not self.source.row.startswith("List item "):
            return message == self.message
        parts = message.split("\n", 2)
        if len(parts) != 3:
            return False
        heading, title, body = parts
        return heading == self.heading and bool(title.strip()) and title.splitlines() == [title] and body == self.message_body


@dataclass(frozen=True)
class QuestionDeliveryTarget:
    path: Path
    item: str
    recipient: staff.StaffId
    row: str
    title: str
    answer_command: str

    def saved(self, question: question_record.Question) -> SavedQuestionDelivery:
        row = f"{self.row}/{question.question_id}"
        source = QuestionSource(str(self.path.resolve()), self.item, question.question_id, question.words, row)
        command = self.answer_command.format(item=shlex.quote(self.item), question_id=question.question_id)
        return SavedQuestionDelivery(source, self.recipient, self.title, command)


def list_question_target(path: Path, item: str, number: int, title: str,
                         recipient: staff.StaffId) -> QuestionDeliveryTarget:
    command = ('~/.claude/scripts/lib/py ~/.claude/scripts/production/the_list.py '
               + 'answer {item} --question {question_id} --text "<answer>"')
    return QuestionDeliveryTarget(path, item, recipient, f"List item {number}", title, command)


def image_question_target(path: Path, image: image_store.ImageRecord) -> QuestionDeliveryTarget:
    command = ('~/.claude/scripts/lib/py ~/.claude/scripts/review_links/images.py answer '
               + '--id {item} --question {question_id} --text "<answer>"')
    owner = staff.StaffId(image["owner"])
    return QuestionDeliveryTarget(path, image["id"], owner, IMAGE_QUESTION_ROW_PREFIX + image["id"],
                                  image["caption"], command)


@dataclass(frozen=True)
class VerifiedQuestionAttempt:
    """Bind a delivery attempt to the saved question that authorizes it."""

    record: QuestionAttempt
    saved: SavedQuestionDelivery


@dataclass(frozen=True)
class QuestionJob:
    """Keep the claim lock open from the claim through result publication."""

    delivery: VerifiedQuestionAttempt
    lock: IO[str]
    start: threading.Lock = field(default_factory=threading.Lock)
    release: Callable[[], None] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "release", finalize(self, self.lock.close))

    @property
    def record(self) -> QuestionAttempt:
        return self.delivery.record


@dataclass(frozen=True)
class PageQuestionSend(MessageDelivery):
    job: QuestionJob | NoQuestionJob = NO_QUESTION_JOB


def _boot_id() -> str:
    if sys.platform == "darwin":
        return subprocess.check_output(["sysctl", "-n", "kern.boottime"], text=True, timeout=5).strip()
    return boot_identity()


def _question_lock_path(path: Path) -> Path:
    return path.parent / ".review-link-question-jobs" / path.name


type QuestionLockState = Literal["held", "released", "unreadable"]
type QuestionJobObservation = QuestionLockState | Literal["unobserved"]

_local_question_jobs: WeakValueDictionary[Path, IO[str]] = WeakValueDictionary()


def _published_question_job(path: Path) -> Literal["held", "unobserved"]:
    """Observe a job in this process without taking its lock."""
    job = _local_question_jobs.get(path.resolve())
    return "held" if job is not None and not job.closed else "unobserved"


def _question_lock_state(path: Path) -> QuestionLockState:
    try:
        with path.open("r", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return "held"
        return "released"
    except FileNotFoundError:
        return "released"
    except OSError:
        return "unreadable"


def _question_owner_matches(owner: QuestionSendOwner) -> bool:
    try:
        if owner.host != socket.gethostname() or owner.boot_id != _boot_id():
            return False
        start = process_start.process_start(owner.pid)
        return isinstance(start, process_start.Started) and start.start == owner.process_start
    except (OSError, subprocess.SubprocessError):
        return False


@dataclass(frozen=True)
class PageAttempt(QuestionAttempt):
    progress: QuestionProgress | None = None
    legacy_key: str = ""


def page_progress(record: QuestionAttempt) -> QuestionProgress:
    if isinstance(record, PageAttempt) and record.progress is not None:
        return record.progress
    return QuestionProgress(record.attempt_id, record.source.question_id, time.time(),
                            state=page_state(record), error=record.error)


def page_state(record: QuestionAttempt) -> QuestionDeliveryState:
    if record.state in (QuestionSendState.SENT, QuestionSendState.QUEUED):
        return QuestionDeliveryState.CONFIRMED
    if record.state is QuestionSendState.FAILED:
        return QuestionDeliveryState.FAILED
    if record.earlier_unconfirmed or record.state in (QuestionSendState.SENDING, QuestionSendState.UNKNOWN):
        return QuestionDeliveryState.UNKNOWN
    return QuestionDeliveryState.NOT_STARTED


def decode_page_attempt(fields: dict[str, object]) -> PageAttempt:
    value = fields.get("question_attempt")
    if "question_attempt" not in fields:
        raise QuestionAttemptMissing()
    if not isinstance(value, dict):
        raise ValueError("Invalid question delivery record.")
    data = cast(dict[str, object], value)
    if data.get("schema") != 3:
        raise ValueError("The question delivery record needs migration.")
    progress = read_question_progress(data.get("delivery"))
    if progress.state is QuestionDeliveryState.CONFIRMED:
        receipt = data.get("receipt")
        queued = isinstance(receipt, dict) and cast(dict[str, object], receipt).get("outcome") == "queued"
        state = QuestionSendState.QUEUED if queued else QuestionSendState.SENT
    elif progress.state is QuestionDeliveryState.NOT_STARTED:
        state = QuestionSendState.CLAIMED if data.get("owner") is not None else QuestionSendState.READY
    elif progress.state is QuestionDeliveryState.FAILED:
        state = QuestionSendState.FAILED
    else:
        state = QuestionSendState.SENDING if data.get("owner") is not None else QuestionSendState.UNKNOWN
    legacy_key = data.get("legacy_key", "")
    if not isinstance(legacy_key, str):
        raise ValueError("Invalid legacy question key.")
    attempt = hashlib.sha256(progress.attempt.encode()).hexdigest() if legacy_key else progress.attempt
    record = delivery_state.decode_question_attempt({"question_attempt": {
        **data, "schema": 2, "state": state, "attempt_id": attempt, "error": progress.error}})
    if record.source.question_id != progress.question_id:
        raise ValueError("The delivery record has a different question ID.")
    if legacy_key and legacy_key != review_delivery_key(Path(record.source.path), record.source.row, progress.attempt):
        raise ValueError("The delivery record has a different legacy key.")
    return PageAttempt(record.source, record.recipient_id, record.message, record.revision, record.state,
                       progress.attempt, record.owner, record.receipt, record.error, record.earlier_unconfirmed,
                       record.resend_requests, progress, legacy_key)


def read_question_attempt(path: Path) -> PageAttempt:
    return decode_page_attempt(read_delivery_object(path))


@dataclass(frozen=True)
class NoQuestionAttempt:
    """The delivery file has no valid current attempt."""


@dataclass(frozen=True)
class QuestionSendPermitted:
    state: Literal[QuestionSendState.READY]
    attempt: QuestionAttempt | NoQuestionAttempt


@dataclass(frozen=True)
class ResendBlocked:
    reason: str
    attempt: QuestionAttempt | NoQuestionAttempt
    state: Literal[QuestionSendState.UNKNOWN, QuestionSendState.CLAIMED, QuestionSendState.SENDING] = QuestionSendState.UNKNOWN


@dataclass(frozen=True)
class QuestionDeliveryUnknown:
    attempt: QuestionAttempt | NoQuestionAttempt = NoQuestionAttempt()
    state: Literal[QuestionSendState.UNKNOWN] = field(default=QuestionSendState.UNKNOWN, init=False)


@dataclass(frozen=True)
class QuestionDeliveryAccepted:
    state: Literal[QuestionSendState.SENT, QuestionSendState.QUEUED]
    attempt: QuestionAttempt | NoQuestionAttempt


@dataclass(frozen=True)
class QuestionDeliveryFailed:
    attempt: QuestionAttempt | NoQuestionAttempt
    state: Literal[QuestionSendState.FAILED] = QuestionSendState.FAILED


type QuestionDeliveryView = (QuestionSendPermitted | ResendBlocked | QuestionDeliveryUnknown
                             | QuestionDeliveryAccepted | QuestionDeliveryFailed)


_QUESTION_SENDER_UNVERIFIED = "The server cannot verify the question sender."
_QUESTION_SENDER_ACTIVE = "The previous question sender is still active."


def question_delivery_view(status: DeliveryStatus, saved: SavedQuestionDelivery) -> QuestionDeliveryView:
    """Check delivery eligibility under the delivery lock."""
    return _question_delivery_view(status, saved, _question_lock_state)


def published_question_delivery_view(status: DeliveryStatus, saved: SavedQuestionDelivery) -> QuestionDeliveryView:
    """Read the published attempt for display."""
    return _question_delivery_view(status, saved, _published_question_job)


def _question_delivery_view(status: DeliveryStatus, saved: SavedQuestionDelivery,
                            observe_job: Callable[[Path], QuestionJobObservation]) -> QuestionDeliveryView:
    page = saved.page()
    path = status.path(page, saved.source.row)
    lock = observe_job(_question_lock_path(path))
    record: QuestionAttempt | NoQuestionAttempt = NoQuestionAttempt()
    owner: QuestionSendOwner | NoQuestionSendOwner = NoQuestionSendOwner()
    unreadable = False
    try:
        fields = delivery_state.read_fields(path)
        record = decode_page_attempt(fields)
        same_question = record.source == saved.source and record.recipient_id == saved.recipient
        failed = same_question and page_progress(record).state is QuestionDeliveryState.FAILED
        matches = saved.matches(record)
        state = record.state if failed or matches else QuestionSendState.UNKNOWN
        if state is QuestionSendState.FAILED and not failed:
            state = QuestionSendState.UNKNOWN
        if failed and not matches:
            return QuestionDeliveryFailed(record)
        owner = record.owner
    except FileNotFoundError:
        state = QuestionSendState.READY
    except (OSError, ValueError):
        state = QuestionSendState.UNKNOWN
        unreadable = True
    if state is QuestionSendState.SENT or state is QuestionSendState.QUEUED:
        return QuestionDeliveryAccepted(state, record)
    if unreadable:
        return ResendBlocked("The server cannot read the question delivery record.", record)
    if lock == "unreadable":
        return ResendBlocked(_QUESTION_SENDER_UNVERIFIED, record)
    if lock == "held":
        if (state is QuestionSendState.CLAIMED or state is QuestionSendState.SENDING) and isinstance(owner, QuestionSendOwner):
            if _question_owner_matches(owner):
                return ResendBlocked(_QUESTION_SENDER_ACTIVE, record, state)
            return ResendBlocked(_QUESTION_SENDER_UNVERIFIED, record)
        return ResendBlocked(_QUESTION_SENDER_ACTIVE, record)
    if state in (QuestionSendState.CLAIMED, QuestionSendState.READY):
        return QuestionSendPermitted(QuestionSendState.READY, record)
    if state is QuestionSendState.FAILED:
        return QuestionDeliveryFailed(record)
    return QuestionDeliveryUnknown(record)


def publish_question_attempt(path: Path, record: QuestionAttempt) -> None:
    progress = page_progress(record)
    data = {"schema": 3, "message_id": record.message_id, "source": asdict(record.source),
            "message": record.message, "recipient_id": record.recipient_id, "revision": record.revision,
            "delivery": progress.stored(),
            "owner": asdict(record.owner) if isinstance(record.owner, QuestionSendOwner) else None,
            "receipt": asdict(record.receipt) if isinstance(record.receipt, QuestionSendReceipt) else None}
    if isinstance(record, PageAttempt) and record.legacy_key:
        data["legacy_key"] = record.legacy_key
    content = json.dumps({"question_attempt": data}) + "\n"
    try:
        atomic_file.write_text_atomic(path, content, new_file_mode=0o600)
    except atomic_file.AtomicDurabilityUnconfirmed:
        if delivery_state.read_fields(path) != {"question_attempt": data}:
            raise


class StaleQuestionAttempt(ValueError):
    """The attempt no longer has the record that authorized the next write."""


class ClosedQuestionSource(ValueError):
    """The question is closed, answered, or absent."""

    def __init__(self, reason: str, state: QuestionDeliveryState | None = None) -> None:
        super().__init__(reason)
        self.state: QuestionDeliveryState = QuestionDeliveryState.CLOSED if state is None else state


class QuestionRecoveryEnded(ValueError):
    """The delivery attempt has a terminal recovery result."""


class QuestionNotStarted(RuntimeError):
    """The route stopped before it could send the question."""


class QuestionSendStopped(RuntimeError):
    """This send has a final result that proves non-acceptance."""


def _require_current_question_attempt(path: Path, expected: QuestionAttempt) -> None:
    try:
        current = read_question_attempt(path)
    except FileNotFoundError as error:
        raise StaleQuestionAttempt("This question delivery job is stale.") from error
    if current != expected:
        raise StaleQuestionAttempt("This question delivery job is stale.")


def advance_question_attempt(path: Path, expected: QuestionAttempt, record: QuestionAttempt) -> None:
    """Replace the expected record only if it has not changed."""
    _require_current_question_attempt(path, expected)
    changed = replace(record, revision=expected.revision + 1, resend_requests=())
    if isinstance(changed, PageAttempt) and changed.progress is not None and record.state != expected.state:
        changed = replace(changed, progress=replace(changed.progress, state=page_state(changed)))
    publish_question_attempt(path, changed)


def question_envelope(page: ReviewPage, record: QuestionAttempt,
              job: QuestionJob | NoQuestionJob = NO_QUESTION_JOB) -> PageQuestionSend:
    return PageQuestionSend(page, record.source.row, (record.recipient_id,), record.message,
                            record.attempt_id, (record.recipient_id,), job=job)


def claim_question(status: DeliveryStatus, delivery: VerifiedQuestionAttempt) -> PageQuestionSend:
    record = delivery.record
    page = delivery.saved.page()
    path = status.path(page, delivery.saved.source.row)
    lock_path = _question_lock_path(path)
    lock: IO[str] | None = None
    try:
        start = process_start.process_start(os.getpid())
        boot_id = _boot_id()
        if not isinstance(start, process_start.Started) or not start.start.strip() or not boot_id.strip():
            raise OSError("Cannot read this question sender's process identity.")
        owner = QuestionSendOwner(socket.gethostname(), boot_id, os.getpid(), start.start, secrets.token_hex(32))
        lock_path.parent.mkdir(exist_ok=True)
        lock = lock_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            status.defer_question(path)
            return question_envelope(page, record)
        _local_question_jobs[lock_path.resolve()] = lock
        claimed = replace(record, revision=record.revision + 1, state=QuestionSendState.CLAIMED, owner=owner)
        advance_question_attempt(path, record, claimed)
        status.question_jobs.add(lock)
        return question_envelope(page, claimed, QuestionJob(replace(delivery, record=claimed), lock))
    except BaseException as error:
        if lock is not None:
            lock.close()
        if not isinstance(error, Exception):
            raise
        _defer_question_error(status, path, record.source.row, error)
        return question_envelope(page, record)


def ready_and_claim_question(status: DeliveryStatus, record: QuestionAttempt,
                             saved: SavedQuestionDelivery) -> PageQuestionSend:
    if not saved.matches(record):
        raise question_record.AnswerRefused("The saved question does not match this delivery.")
    if page_progress(record).state is not QuestionDeliveryState.NOT_STARTED:
        return question_envelope(saved.page(), record)
    return claim_question(status, VerifiedQuestionAttempt(record, saved))


def question_recipient(recipient: staff.StaffId) -> staff.StaffRow:
    """Check staff before Page and Review question transport."""
    try:
        member = staff.snapshot().by_id(recipient)
    except OSError as error:
        raise QuestionNotStarted(str(error)) from error
    if member is None:
        raise QuestionSendStopped(f"No staff record for {recipient}.")
    return member


def send_question(recipient: staff.StaffId, message: str, message_id: str
                  ) -> tuple[send_result.ConfirmedSendOutcome | send_result.UnconfirmedSendOutcome, str]:
    member = question_recipient(recipient)
    return question_transport(recipient, member.kind, SENDER, message, message_id)


def question_send_result(outcome: send_result.ConfirmedSendOutcome | send_result.UnconfirmedSendOutcome,
                         error: str) -> QuestionSendReceipt:
    if isinstance(outcome, send_result.ConfirmedSendOutcome):
        header = outcome.header
        if header.outcome in ("sent", "skipped"):
            return QuestionSendReceipt("kept" if header.outcome == "skipped" else "sent", header.detail)
        if header.outcome == "refused":
            raise QuestionSendStopped(error)
        if header.outcome == "retry" and header.payload == QUESTION_NOT_STARTED:
            raise QuestionNotStarted(error)
    raise UnconfirmedSend(error)


_question_transport_deadline: ContextVar[float | None] = ContextVar("question_transport_deadline", default=None)
_transport_claim: ContextVar[int | None] = ContextVar("transport_claim", default=None)


_transport_started: ContextVar[Callable[[], None] | None] = ContextVar("transport_started", default=None)


def attempt_question_send(progress: QuestionProgress, send: Callable[[], QuestionSendReceipt], *,
                          started: Callable[[], None] | None = None
                          ) -> tuple[QuestionProgress, QuestionSendReceipt | NoQuestionSendReceipt]:
    token = _question_transport_deadline.set(progress.deadline)
    marker = _transport_started.set(started)
    try:
        receipt = send()
    except QuestionSendStopped as error:
        return progress.fail(str(error)), NoQuestionSendReceipt()
    except (QuestionNotStarted, QuestionRecoveryEnded) as error:
        return replace(progress, state=QuestionDeliveryState.NOT_STARTED, error=str(error)), NoQuestionSendReceipt()
    except Exception as error:
        return replace(progress, state=QuestionDeliveryState.UNKNOWN, error=str(error)), NoQuestionSendReceipt()
    finally:
        _transport_started.reset(marker)
        _question_transport_deadline.reset(token)
    return replace(progress, state=QuestionDeliveryState.CONFIRMED, error=""), receipt


def begin_question_send(progress: QuestionProgress) -> QuestionProgress:
    """Record that the sender process started."""
    return replace(progress, state=QuestionDeliveryState.UNKNOWN, error="")


def _list_question_store(record: QuestionAttempt) -> record_store.NumberedRecords:
    return record_store.NumberedRecords(record.recipient_id, "list", "L", "list item", ValueError)


def _read_list_question_item(listing: record_store.NumberedRecords,
                        record: QuestionAttempt) -> dict[str, str]:
    source = record.source
    path = listing.path(source.item_id)
    if path.resolve() != Path(source.path):
        raise ValueError("The List question source has moved.")
    try:
        _ = path.stat()
    except FileNotFoundError as error:
        raise ClosedQuestionSource("The List question source was removed.") from error
    try:
        fields = listing.read(source.item_id, path=path).fields
    except ValueError as error:
        raise OSError(f"Cannot read the List question source: {error}") from error
    if fields.get("id") != source.item_id:
        raise ValueError("The List question source has a different item ID.")
    return fields


def _open_list_question(listing: record_store.NumberedRecords, record: QuestionAttempt) -> dict[str, str]:
    fields = _read_list_question_item(listing, record)
    if fields["state"] == "Closed":
        raise ClosedQuestionSource("The List question source is Closed.")
    return fields


def _read_list_question_source(fields: dict[str, str]) -> tuple[question_record.Question, ...]:
    try:
        return delivery_state.read_list_questions(fields.get("questions", "[]"))
    except ValueError as error:
        raise OSError(f"Cannot read the List questions: {error}") from error


@contextmanager
def _lock_open_question_source(status: DeliveryStatus, record: QuestionAttempt) -> Generator[None, None, None]:
    """Check the saved question before the sender can start."""
    source = record.source
    if source.row.startswith(IMAGE_QUESTION_ROW_PREFIX) and Path(source.path).name == image_store.RECORD:
        with status.locked():
            image = _read_image_question_source(status, record)
            _ = _verify_open_question(record, tuple(question_record.read(question) for question in image.get("questions", [])))
            yield
        return
    if not record.source.row.startswith("List item "):
        yield
        return
    listing = _list_question_store(record)
    with listing.locked():
        fields = _open_list_question(listing, record)
        questions = _read_list_question_source(fields)
        _ = _verify_open_question(record, questions)
        yield


def _verify_open_question(record: QuestionAttempt, questions: tuple[question_record.Question, ...]) -> question_record.Question:
    question = next((question for question in questions if question.question_id == record.source.question_id), None)
    if question is None:
        raise ClosedQuestionSource("The question is no longer open.")
    if question.answer is not None:
        raise ClosedQuestionSource("The question has an answer.", QuestionDeliveryState.ANSWERED)
    if question.words != record.source.words:
        raise ValueError("The saved question has different words.")
    return question


def question_answer_lock(path: Path, question_id: str, *, wait: bool = True
                         ) -> AbstractContextManager[file_lock.LockResult]:
    """Serialize an answer with each send of this question."""
    return file_lock.exclusive(question_answer_lock_path(path, question_id), wait=wait)


def question_answer_lock_path(path: Path, question_id: str) -> Path:
    source = path.resolve()
    key = hashlib.sha256(json.dumps((source.name, question_id)).encode()).hexdigest()
    return source.parent / f".question-answer-{key}.lock"


@contextmanager
def sender_claim(path: Path) -> Generator[bool]:
    claim = file_lock.take_exclusive(path)
    if claim is file_lock.LockResult.BUSY:
        yield False
        return
    token = _transport_claim.set(claim.descriptor)
    try:
        yield True
    finally:
        _transport_claim.reset(token)
        claim.close()


def _defer_question_error(status: DeliveryStatus, path: Path, row: str, error: Exception, *,
                          delivery: DeferredQuestion = None) -> None:
    status.defer_question(path, delivery)
    status.report_recovery_error(path, error, f"question {row}")


NO_QUESTION_SEND_RECEIPT = NoQuestionSendReceipt()


def save_page_progress(path: Path, record: QuestionAttempt, progress: QuestionProgress, *,
                       receipt: QuestionSendReceipt | NoQuestionSendReceipt = NO_QUESTION_SEND_RECEIPT,
                       keep_owner: bool = False
                       ) -> PageAttempt:
    state = {
        QuestionDeliveryState.NOT_STARTED: QuestionSendState.READY,
        QuestionDeliveryState.UNKNOWN: QuestionSendState.UNKNOWN,
        QuestionDeliveryState.CONFIRMED: QuestionSendState.SENT,
        QuestionDeliveryState.ANSWERED: QuestionSendState.UNKNOWN,
        QuestionDeliveryState.CLOSED: QuestionSendState.UNKNOWN,
        QuestionDeliveryState.FAILED: QuestionSendState.FAILED,
    }[progress.state]
    owner = record.owner if keep_owner else NoQuestionSendOwner()
    if isinstance(owner, QuestionSendOwner):
        state = (QuestionSendState.SENDING if progress.state is QuestionDeliveryState.UNKNOWN
                 else QuestionSendState.CLAIMED)
    updated = PageAttempt(record.source, record.recipient_id, record.message, record.revision + 1, state,
                          record.attempt_id, owner=owner, receipt=receipt, error=progress.error, progress=progress,
                          legacy_key=record.legacy_key if isinstance(record, PageAttempt) else "")
    _require_current_question_attempt(path, record)
    publish_question_attempt(path, updated)
    return updated


def retire_page_question(status: DeliveryStatus, path: Path, record: QuestionAttempt,
                         state: QuestionDeliveryState) -> None:
    try:
        with status.locked():
            _ = save_page_progress(path, record, replace(page_progress(record), state=state, error=""))
    except StaleQuestionAttempt:
        return
    except (OSError, ValueError) as error:
        _defer_question_error(status, path, record.source.row, error)


def record_page_question_answer(status: DeliveryStatus, target: QuestionDeliveryTarget,
                                question: question_record.Question) -> None:
    """Record the answer after the source saves it."""
    saved = target.saved(question)
    path = status.path(saved.page(), saved.source.row)
    try:
        _ = path.stat()
        with status.locked():
            record = read_question_attempt(path)
            if saved.matches(record) and page_progress(record).state is not QuestionDeliveryState.ANSWERED:
                retire_page_question(status, path, record, QuestionDeliveryState.ANSWERED)
    except (FileNotFoundError, QuestionAttemptMissing):
        return
    except (OSError, ValueError) as error:
        _defer_question_error(status, path, saved.source.row, error, delivery=UnreadableDeliveryRecord())


def deliver_page_question(delivery: PageQuestionSend, status: DeliveryStatus) -> None:
    """Send once under the claim and store its result."""
    job = delivery.job
    if isinstance(job, NoQuestionJob) or not job.start.acquire(blocking=False):
        return
    record = job.record
    path = status.path(delivery.page, delivery.row)
    try:
        source = record.source
        with question_answer_lock(Path(source.path), source.question_id):
            with _lock_open_question_source(status, record), status.locked():
                _require_current_question_attempt(path, record)
                _ = status.closeout_fields(delivery.page, delivery.row)
                progress, setting_error = page_progress(record).start_budget()
                record = save_page_progress(path, record, progress, keep_owner=True)

            def started() -> None:
                nonlocal record
                with status.locked():
                    record = save_page_progress(path, record, begin_question_send(progress), keep_owner=True)

            if setting_error is None:
                key = record.legacy_key or record.message_id
                token = _transport_claim.set(job.lock.fileno())
                try:
                    result, receipt = attempt_question_send(progress, lambda: question_send_result(
                        *send_question(record.recipient_id, record.message, key)), started=started)
                finally:
                    _transport_claim.reset(token)
            else:
                result, receipt = progress.fail(setting_error), NoQuestionSendReceipt()
            with status.locked():
                record = save_page_progress(path, record, result, receipt=receipt)
                if result.state is QuestionDeliveryState.FAILED:
                    queue_page_question_failure(status, path, record, result)
        if result.state.pending:
            status.defer_question(path)
        else:
            status.forget_deferred_question(path)
    except StaleQuestionAttempt:
        return
    except ClosedQuestionSource as error:
        retire_page_question(status, path, record, error.state)
    except (OSError, ValueError, KeyError, TypeError) as error:
        _defer_question_error(status, path, delivery.row, error)
    finally:
        _ = job.release()
        status.question_jobs.discard(job.lock)


def _read_image_question_source(status: DeliveryStatus, record: QuestionAttempt) -> image_store.ImageRecord:
    source = record.source
    if not image_store.IDENTITY.fullmatch(source.item_id):
        raise ValueError("The Images question source has an invalid item ID.")
    folder = status.root / image_store.IMAGES_FOLDER / source.item_id
    if (folder / image_store.RECORD).resolve() != Path(source.path):
        raise ValueError("The Images question source has moved.")
    try:
        image = image_store.read_record(folder)
    except FileNotFoundError as error:
        raise ClosedQuestionSource("The image was removed.") from error
    except ValueError as error:
        raise OSError(f"Cannot read the Images question source: {error}") from error
    if image["owner"] != record.recipient_id or image["id"] != source.item_id:
        raise ValueError("The Images question source has a different recipient or item ID.")
    return image


def page_source_answered(status: DeliveryStatus, record: QuestionAttempt) -> bool:
    """Use the source answer even when an earlier attempt has a receipt."""
    source = record.source
    try:
        if source.row.startswith("List item "):
            fields = _read_list_question_item(_list_question_store(record), record)
            _ = _verify_open_question(record, _read_list_question_source(fields))
        elif source.row.startswith(IMAGE_QUESTION_ROW_PREFIX) and Path(source.path).name == image_store.RECORD:
            image = _read_image_question_source(status, record)
            _ = _verify_open_question(record, tuple(question_record.read(value) for value in image.get("questions", [])))
    except ClosedQuestionSource as error:
        return error.state is QuestionDeliveryState.ANSWERED
    return False


def queue_page_question_failure(status: DeliveryStatus, path: Path, record: QuestionAttempt,
                                failure: QuestionProgress) -> None:
    """Save a showrunner alert for the recorded failure."""
    source = record.source
    source_path = Path(source.path)
    page = ReviewPage(source_path, source_path.parent, (source.row,), "")
    if source.row.startswith("List item "):
        listing = _list_question_store(record)
        fields = _open_list_question(listing, record)
        _ = _verify_open_question(record, _read_list_question_source(fields))
        owner = staff.StaffId(fields["owner"])
        recipients = tuple(runner.id for runner in staff_showrunner(owner, staff.snapshot()))
    elif source.row.startswith(IMAGE_QUESTION_ROW_PREFIX) and source_path.name == image_store.RECORD:
        image = _read_image_question_source(status, record)
        _ = _verify_open_question(record, tuple(question_record.read(question) for question in image.get("questions", [])))
        recipients = review_alert_showrunners(record.recipient_id)
    else:
        return
    delivery = question_envelope(page, record)
    alerts = pending_delivery_alerts(delivery, [DeliveryFailure(record.recipient_id, failure.error)], 1, recipients,
                                    deadline=failure.deadline)
    if alerts:
        status.store_pending_alerts(delivery, alerts)
        with status.deferred_delivery_lock:
            status.deferred_alerts.add(question_closeout_path(path))


def prepare_page_recovery(status: DeliveryStatus, path: Path, record: QuestionAttempt) -> QuestionAttempt | None:
    progress = page_progress(record)
    if progress.state in (QuestionDeliveryState.ANSWERED, QuestionDeliveryState.CLOSED):
        return None
    source = record.source
    source_path = Path(source.path)
    page = ReviewPage(source_path, source_path.parent, (source.row,), "")
    if status.path(page, source.row) != path:
        raise ValueError("The question delivery path does not match its saved source.")
    lock = _question_lock_path(path)
    with question_answer_lock(Path(record.source.path), record.source.question_id, wait=False) as claim, status.locked():
        if claim is file_lock.LockResult.BUSY:
            status.defer_question(path)
            return None
        _require_current_question_attempt(path, record)
        lock_state = _question_lock_state(lock)
        if lock_state == "held":
            status.defer_question(path)
            return None
        if lock_state == "unreadable":
            raise OSError("Cannot read the question sender lock.")
        if page_source_answered(status, record):
            _ = save_page_progress(path, record, replace(progress, state=QuestionDeliveryState.ANSWERED, error=""))
            status.forget_deferred_question(path)
            return None
        if progress.state is QuestionDeliveryState.CONFIRMED:
            return None
        if progress.deadline is None:
            progress, _ = progress.start_budget()
        expired = progress.expire()
        if expired.state is QuestionDeliveryState.FAILED:
            try:
                queue_page_question_failure(status, path, record, expired)
            except ClosedQuestionSource as error:
                expired = replace(expired, state=error.state, error="")
        if expired != page_progress(record):
            record = save_page_progress(path, record, expired)
        if not expired.state.pending:
            status.forget_deferred_question(path)
            return None
        return record


def recover_page_question(status: DeliveryStatus, path: Path, record: QuestionAttempt) -> None:
    """Expire the question or claim a send that did not start."""
    source = record.source
    source_path = Path(source.path)
    try:
        try:
            _ = source_path.parent.stat()
        except FileNotFoundError as error:
            raise ClosedQuestionSource("The question source folder was removed.") from error
        ready = prepare_page_recovery(status, path, record)
        if ready is None:
            return
        record = ready
        delivery = _claim_recovered_page_question(status, record, source_path)
        if page_progress(record).state is QuestionDeliveryState.UNKNOWN:
            status.defer_question(path)
    except StaleQuestionAttempt:
        return
    except ClosedQuestionSource as error:
        retire_page_question(status, path, record, error.state)
        return
    except (OSError, ValueError, KeyError, TypeError) as error:
        _defer_question_error(status, path, source.row, error)
        return
    deliver(delivery, status)


def _claim_recovered_page_question(status: DeliveryStatus, record: QuestionAttempt,
                                   source_path: Path) -> MessageDelivery | NoMessageDelivery:
    source = record.source
    if (source.row == f"{IMAGE_QUESTION_ROW_PREFIX}{source.item_id}/{source.question_id}"
            and source_path.name == image_store.RECORD):
        with status.locked():
            image = _read_image_question_source(status, record)
            questions = tuple(question_record.read(question) for question in image.get("questions", []))
            target = image_question_target(source_path, image)
            return _recover_saved_question(status, target, record, questions)
    elif source.row == f"List item {source.item_id.removeprefix('L')}/{source.question_id}":
        listing = _list_question_store(record)
        with listing.locked(), status.locked():
            fields = _open_list_question(listing, record)
            questions = _read_list_question_source(fields)
            target = list_question_target(source_path, fields["id"], int(fields["number"]), fields["title"],
                                          record.recipient_id)
            return _recover_saved_question(status, target, record, questions)
    return NoMessageDelivery()


def _recover_saved_question(status: DeliveryStatus, target: QuestionDeliveryTarget, record: QuestionAttempt,
                            questions: tuple[question_record.Question, ...]) -> MessageDelivery | NoMessageDelivery:
    question = _verify_open_question(record, questions)
    saved = target.saved(question)
    if not saved.matches(record):
        raise ValueError("The saved question does not match this delivery.")
    page = saved.page()
    path = status.path(page, saved.source.row)
    _require_current_question_attempt(path, record)
    return ready_and_claim_question(status, record, saved)


def stored_intent(intent: DeliveryIntent) -> StoredDeliveryIntent:
    return {"attempt": intent.attempt, "state": "pending", "recipients": list(intent.recipients)}


@dataclass(frozen=True)
class AttemptOwnership:
    """Own an attempt from its claim until its delivery ends."""

    row_file: Path
    state: Literal["claimed", "sending"]
    request_thread: threading.Thread = field(default_factory=threading.current_thread)


@dataclass(frozen=True)
class UnreadableDeliveryRecord:
    """Read this delivery record again before selecting its recovery path."""


@dataclass(frozen=True)
class ListFeedbackRetry:
    deliver: Callable[[], None]


type DeferredQuestion = MessageDelivery | UnreadableDeliveryRecord | ListFeedbackRetry | None


class QuestionDeliveryState(StrEnum):
    NOT_STARTED = "not_started"
    UNKNOWN = "unknown"
    CONFIRMED = "confirmed"
    ANSWERED = "answered"
    CLOSED = "closed"
    FAILED = "failed"

    @property
    def pending(self) -> bool:
        return self in (self.NOT_STARTED, self.UNKNOWN)


class StoredQuestionProgress(TypedDict):
    attempt: str
    question_id: str
    saved_at: float
    deadline: float | None
    state: QuestionDeliveryState
    error: str
    confirmed: list[str]
    recipients: NotRequired[list[str]]


@dataclass(frozen=True)
class QuestionProgress:
    attempt: str
    question_id: str
    saved_at: float
    deadline: float | None = None
    state: QuestionDeliveryState = QuestionDeliveryState.NOT_STARTED
    error: str = ""
    confirmed: tuple[str, ...] = ()
    recipients: tuple[str, ...] | None = None

    def stored(self) -> StoredQuestionProgress:
        fields: StoredQuestionProgress = {"attempt": self.attempt, "question_id": self.question_id,
            "saved_at": self.saved_at, "deadline": self.deadline, "state": self.state, "error": self.error,
            "confirmed": list(self.confirmed)}
        if self.recipients is not None:
            fields["recipients"] = list(self.recipients)
        return fields

    def start_budget(self) -> tuple[QuestionProgress, str | None]:
        setting_error = None
        try:
            seconds = conf.read("review_links").value(
                "question_recovery_seconds", question_recovery_seconds, default=QUESTION_RECOVERY_DEFAULT_SECONDS,
                recover_invalid=False)
        except (OSError, conf.Unusable) as error:
            setting_error = " ".join(str(error).splitlines())
            print(f"review-links question {self.question_id}: {setting_error}", file=sys.stderr)
            seconds = QUESTION_RECOVERY_DEFAULT_SECONDS
        if self.deadline is not None:
            return self, setting_error
        return replace(self, deadline=self.saved_at + seconds), setting_error

    def fail(self, error: str) -> QuestionProgress:
        if self.confirmed:
            return replace(self, state=QuestionDeliveryState.CONFIRMED, error="")
        return replace(self, state=QuestionDeliveryState.FAILED, error=error)

    def expire(self) -> QuestionProgress:
        if self.state.pending and self.deadline is not None and time.time() >= self.deadline:
            return self.fail(QUESTION_RECOVERY_EXPIRED)
        return self


def read_question_progress(value: object) -> QuestionProgress:
    fields = record_field({"question_delivery": value}, "question_delivery")
    attempt, question_id, error = (fields.get(key) for key in ("attempt", "question_id", "error"))
    saved_at, deadline, state, confirmed = (fields.get(key) for key in ("saved_at", "deadline", "state", "confirmed"))
    if (not isinstance(attempt, str) or not isinstance(question_id, str) or not isinstance(error, str)
            or not isinstance(state, str) or not isinstance(saved_at, (int, float))
            or isinstance(saved_at, bool) or not delivery_number(saved_at)
            or (deadline is not None and (not isinstance(deadline, (int, float))
                                         or isinstance(deadline, bool) or not delivery_number(deadline)))
            or not isinstance(confirmed, list)
            or not all(isinstance(recipient, str) for recipient in cast(list[object], confirmed))):
        raise ValueError("Invalid question delivery record.")
    return QuestionProgress(attempt, question_id, saved_at, deadline, QuestionDeliveryState(state), error,
                            tuple(cast(list[str], confirmed)), migrated_recipients(fields))


def question_closeout_path(path: Path) -> Path:
    return path.with_name(CLOSEOUT_PREFIX + path.name.removeprefix(DELIVERY_PREFIX) + ".json")


def question_recovery_seconds(value: str) -> int:
    seconds = int(value)
    if not 1 <= seconds <= 86400:
        raise ValueError("expected 1 to 86400 seconds")
    return seconds


class DeliveryStatus:
    """Own delivery intents and serialize conditional attempt updates."""

    def __init__(self, root: Path) -> None:
        self.root: Path = root
        self.request_attempts: dict[str, AttemptOwnership] = {}
        self.question_jobs: WeakSet[IO[str]] = WeakSet()
        self.startup_recovery_pending: threading.Event = threading.Event()
        self.delivery_migration_done: bool = False
        self.question_sources_pending: threading.Event = threading.Event()
        self.deferred_questions: dict[Path, DeferredQuestion] = {}
        self.deferred_migrations: set[Path] = set()
        self.deferred_cleanups: set[Path] = set()
        self.deferred_alerts: set[Path] = set()
        self.recovery_errors: set[tuple[Path, str]] = set()
        self.deferred_delivery_lock: threading.Lock = threading.Lock()

    def report_recovery_error(self, path: Path, error: Exception, label: str) -> None:
        detail = " ".join(str(error).splitlines())
        with self.deferred_delivery_lock:
            key = path, detail
            if key in self.recovery_errors:
                return
            self.recovery_errors.add(key)
        print(f"review-links {label}: {detail}", file=sys.stderr)

    def defer_source(self, path: Path, error: Exception) -> None:
        self.question_sources_pending.set()
        self.report_recovery_error(path, error, f"question source {path}")

    def has_deferred_questions(self) -> bool:
        with self.deferred_delivery_lock:
            return self.question_sources_pending.is_set() or bool(
                self.deferred_questions or self.deferred_migrations or self.deferred_cleanups)

    def defer_cleanup(self, path: Path, error: Exception) -> None:
        with self.deferred_delivery_lock:
            self.deferred_cleanups.add(path)
        self.report_recovery_error(path, error, f"verdict cleanup {path.name}")

    def take_deferred_cleanups(self) -> set[Path]:
        with self.deferred_delivery_lock:
            cleanups, self.deferred_cleanups = self.deferred_cleanups, set()
        return cleanups

    def pending_migrations(self) -> frozenset[Path]:
        with self.deferred_delivery_lock:
            return frozenset(self.deferred_migrations)

    def migration_pending(self, path: Path) -> bool:
        with self.deferred_delivery_lock:
            return path in self.deferred_migrations

    def recovery_paths(self) -> frozenset[Path]:
        return self.read_view().paths - self.pending_migrations()

    def defer_question(self, path: Path, delivery: DeferredQuestion = None) -> None:
        with self.deferred_delivery_lock:
            if isinstance(delivery, UnreadableDeliveryRecord):
                _ = self.deferred_questions.setdefault(path, delivery)
            else:
                self.deferred_questions[path] = delivery

    def forget_deferred_question(self, path: Path) -> None:
        with self.deferred_delivery_lock:
            _ = self.deferred_questions.pop(path, None)

    def take_deferred_questions(self) -> dict[Path, DeferredQuestion]:
        with self.deferred_delivery_lock:
            deliveries = self.deferred_questions
            self.deferred_questions = {}
        return deliveries

    def has_deferred_alerts(self) -> bool:
        with self.deferred_delivery_lock:
            return bool(self.deferred_alerts)

    def recover_deferred_alerts(self) -> None:
        with self.deferred_delivery_lock:
            paths, self.deferred_alerts = self.deferred_alerts, set()
        for path in paths:
            self.send_pending_alerts(path)

    def read_view(self) -> DeliveryReadView:
        return DeliveryReadView(self, frozenset(self.root.glob(DELIVERY_PREFIX + "*")))

    @contextmanager
    def locked(self) -> Generator[None, None, None]:
        # For native Review records, acquire the source lock before the delivery lock.
        self.root.mkdir(parents=True, exist_ok=True)
        with change_review.review_delivery_lock(self.root):
            yield

    @contextmanager
    def native_action(self, page: ReviewPage, row: str) -> Generator[None]:
        """Serialize native actions that use separate source and delivery writes."""
        self.root.mkdir(parents=True, exist_ok=True)
        with question_answer_lock(self.path(page, row), ""):
            yield

    def path(self, page: ReviewPage, row: str) -> Path:
        return change_routing.delivery_path(self.root, page.review_list, row)

    def owns_live_claim(self, page: ReviewPage, row: str, attempt: str) -> bool:
        owner = self.request_attempts.get(attempt)
        return owner is not None and owner.row_file == self.path(page, row) and owner.request_thread.is_alive()

    def release_recovery_claims(self, attempts: tuple[str, ...]) -> None:
        """Release this recovery thread's claims after its sends end."""
        if not attempts:
            return
        recovery_thread = threading.current_thread()
        with self.locked():
            for attempt in attempts:
                owner = self.request_attempts.get(attempt)
                if owner is not None and owner.request_thread is recovery_thread:
                    del self.request_attempts[attempt]

    def closeout_path(self, page: ReviewPage, row: str) -> Path:
        return question_closeout_path(self.path(page, row))

    def review_save_receipt(self, page: ReviewPage, row: str, intent: DeliveryIntent | None = None) -> Path:
        name = self.path(page, row).name.removeprefix(DELIVERY_PREFIX)
        if intent is not None and intent.kind == "question":
            name += "-" + hashlib.sha256(intent.attempt.encode()).hexdigest()
        return self.root / (".review-link-saved-" + name)

    def mark_review_saved(self, page: ReviewMessagePage | UserQuestionPage, row: str, slots: DeliverySlots,
                          intent: DeliveryIntent) -> None:
        """Keep proof of a saved action while the caller holds the delivery lock."""
        try:
            self.write_slots(page, row, slots)
        except Exception as error:
            print(f"review-links delivery save mark {row}: {error}", file=sys.stderr)
            self.write_save_receipt(page, row, intent)

    def write_save_receipt(self, page: ReviewPage, row: str, intent: DeliveryIntent) -> None:
        try:
            atomic_file.write_text_atomic(self.review_save_receipt(page, row, intent), intent.attempt + "\n",
                                          new_file_mode=0o600)
        except OSError as failure:
            print(f"review-links delivery save receipt {row}: {failure}", file=sys.stderr)

    def has_review_save_receipt(self, page: ReviewPage, row: str, intent: DeliveryIntent | NoDelivery) -> bool:
        """Check that the receipt confirms this delivery attempt."""
        if not isinstance(intent, DeliveryIntent):
            return False
        try:
            receipt = self.review_save_receipt(page, row, intent).read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return False
        return receipt == intent.attempt

    def failed_delivery(self, page: ReviewPage, row: str) -> FailedDelivery | NoFailedDelivery:
        """Read the saved failure for a feedback or verdict send."""
        failed = self.closeout_fields(page, row).get("failed_delivery")
        if failed is None:
            return NoFailedDelivery()
        return FailedDelivery(failed["attempt"], Path(failed["review_list"]), failed["row"], failed["last_error"])

    def closeout_fields(self, page: ReviewPage, row: str) -> StoredDeliveryCloseout:
        try:
            return read_closeout_fields(self.closeout_path(page, row))
        except FileNotFoundError:
            return {"review_list": str(page.review_list.resolve()), "row": row, "messages": []}

    @staticmethod
    def write_closeout(path: Path, fields: StoredDeliveryCloseout) -> None:
        atomic_file.write_bytes_atomic(path, (json.dumps(fields) + "\n").encode("utf-8"), mode=0o600)

    def record_result_locked(self, delivery: MessageDelivery, result: FailedDelivery | NoFailedDelivery,
                             alerts: tuple[PendingDeliveryAlert, ...] = ()) -> None:
        """Keep alert history and the delivery result while the caller holds the delivery lock."""
        path = self.closeout_path(delivery.page, delivery.row)
        fields = self.closeout_fields(delivery.page, delivery.row)
        dispatch = fields.get("review_dispatch")
        if dispatch is not None and dispatch["attempt"] == delivery.attempt:
            dispatch["state"] = "finished"
        if alerts:
            fields["pending_alerts"] = [*fields.get("pending_alerts", []), *alerts]
        if isinstance(result, FailedDelivery):
            fields["failed_delivery"] = {"attempt": result.attempt, "review_list": str(result.review_list.resolve()),
                                         "row": result.row, "last_error": result.last_error}
        else:
            _ = fields.pop("failed_delivery", None)
            if (not fields["messages"] and dispatch is None and "question_dispatch" not in fields
                    and not fields.get("pending_alerts")):
                path.unlink(missing_ok=True)
                return
        self.write_closeout(path, fields)

    def store_pending_alerts(self, delivery: MessageDelivery, alerts: tuple[PendingDeliveryAlert, ...]) -> None:
        """Store one alert for each saved key and recipient."""
        page, row = delivery.page, delivery.row
        path = self.closeout_path(page, row)
        fields = self.closeout_fields(page, row)
        pending = fields.setdefault("pending_alerts", [])
        identities = {(alert.get("key"), alert["recipient"]) for alert in pending}
        added = [alert for alert in alerts if (alert.get("key"), alert["recipient"]) not in identities]
        if added:
            pending.extend(added)
            self.write_closeout(path, fields)

    def send_pending_alerts(self, path: Path) -> None:
        """Hold the file claim until the sender exits and the write completes."""
        try:
            claim = file_lock.take_exclusive(path.with_suffix(".lock"))
            if claim is file_lock.LockResult.BUSY:
                retry = True
            else:
                token = _transport_claim.set(claim.descriptor)
                try:
                    retry = self._send_pending_alerts(path)
                finally:
                    _transport_claim.reset(token)
                    claim.close()
        except FileNotFoundError:
            retry = False
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
            retry = True
            self.report_recovery_error(path, error, f"delivery alert {path.name}")
        if retry:
            with self.deferred_delivery_lock:
                self.deferred_alerts.add(path)

    def migrate_alerts(self, path: Path, deadline: float | None = None) -> StoredDeliveryCloseout:
        """Set one expiry for alerts that lack delivery evidence."""
        saved = read_closeout_fields(path)
        alerts = saved.get("pending_alerts", [])
        if all("deadline" in alert for alert in alerts):
            return saved
        if deadline is None:
            deadline = question_delivery_deadline()
        pending: list[PendingDeliveryAlert] = []
        for alert in alerts:
            if "deadline" in alert:
                pending.append(alert)
                continue
            state = alert.get("state", QuestionDeliveryState.UNKNOWN)
            pending.append({**alert, "deadline": deadline, "state": state})
        saved["pending_alerts"] = pending
        self.write_closeout(path, saved)
        return saved

    def _send_pending_alerts(self, path: Path) -> bool:
        retry = False
        with self.locked():
            saved = self.migrate_alerts(path)
        for alert in saved.get("pending_alerts", []):
            state = alert.get("state")
            if state in (QuestionDeliveryState.CONFIRMED, QuestionDeliveryState.FAILED):
                continue
            if state not in (None, QuestionDeliveryState.NOT_STARTED, QuestionDeliveryState.UNKNOWN):
                raise ValueError("Invalid pending alert state.")
            deadline = alert.get("deadline")
            if deadline is None:
                raise ValueError("The pending alert has no deadline.")
            if time.time() >= deadline:
                self._update_pending_alert(path, alert, {**alert, "state": QuestionDeliveryState.FAILED})
                print(f"review-links delivery alert {path.name}: {QUESTION_RECOVERY_EXPIRED}", file=sys.stderr)
                continue
            if state == QuestionDeliveryState.UNKNOWN:
                retry = True
                continue
            key = alert.get("key") or review_delivery_key(Path(saved["review_list"]), saved["row"],
                                                          alert["message"], purpose="alert")
            ready: PendingDeliveryAlert = {**alert, "key": key, "state": QuestionDeliveryState.NOT_STARTED}
            self._update_pending_alert(path, alert, ready)
            current = ready

            def started() -> None:
                nonlocal current
                marked: PendingDeliveryAlert = {**ready, "state": QuestionDeliveryState.UNKNOWN}
                try:
                    self._update_pending_alert(path, ready, marked)
                except atomic_file.AtomicDurabilityUnconfirmed:
                    current = marked
                    raise
                current = marked

            progress = QuestionProgress(key, key, time.time(), deadline)
            result, _ = attempt_question_send(progress, lambda: send_review_question(
                ready["recipient"], ready["message"], key), started=started)
            result = result.expire()
            self._update_pending_alert(path, current, {**current, "state": result.state})
            if result.error and result.state is QuestionDeliveryState.FAILED:
                print(f"review-links delivery alert {path.name}: {result.error}", file=sys.stderr)
            retry |= result.state.pending
        return retry

    def _update_pending_alert(self, path: Path, previous: PendingDeliveryAlert,
                              current: PendingDeliveryAlert) -> None:
        with self.locked():
            fields = read_closeout_fields(path)
            alerts = fields["pending_alerts"] if "pending_alerts" in fields else []
            alerts[alerts.index(previous)] = current
            self.write_closeout(path, fields)

    def notify_closeout_failure(self, page: ReviewPage, row: str, message: str,
                                recipients: tuple[staff.StaffId, ...]) -> None:
        """Send each row's failure once, including across server restarts."""
        if not recipients:
            return
        path = self.closeout_path(page, row)
        with self.locked():
            fields = self.closeout_fields(page, row)
            if message not in fields["messages"]:
                key = review_delivery_key(page.review_list, row, message, purpose="closeout")
                deadline = question_delivery_deadline()
                alerts = fields.setdefault("pending_alerts", [])
                alerts.extend({"recipient": identity, "message": message, "key": key, "deadline": deadline,
                               "state": QuestionDeliveryState.NOT_STARTED} for identity in recipients)
                fields["messages"].append(message)
                self.write_closeout(path, fields)
        self.send_pending_alerts(path)

    def slots(self, page: ReviewPage, row: str) -> DeliverySlots:
        return read_change_delivery(self.path(page, row))

    def validators(self, page: ReviewPage, row: str) -> tuple[staff.StaffId, ...]:
        try:
            fields = checked_delivery_fields(self.path(page, row))
        except FileNotFoundError:
            return ()
        return validation_recipients(fields)

    def metadata_page(self, page: ReviewPage) -> ReviewPage:
        """Preserve the saved message when an answer changes its delivery slots."""
        try:
            fields = cast(StoredDeliverySlots, cast(object, checked_delivery_fields(self.path(page, page.rows[0]))))
        except FileNotFoundError:
            return page
        if 'user_answer' in fields:
            saved = fields['user_answer']
            return UserQuestionPage(page.review_list, Path(saved['production']), page.rows, page.token, saved)
        if 'review_message' in fields:
            return ReviewMessagePage(page.review_list, page.production, page.rows, page.token, fields['review_message'])
        return page

    def write_slots(self, page: ReviewPage, row: str, slots: DeliverySlots, *, new_handoff: bool = False,
                    question_progress: QuestionProgress | None = None) -> None:
        """Write both slots while the caller holds the delivery lock."""
        path = self.path(page, row)
        try:
            previous = checked_delivery_fields(path)
        except FileNotFoundError:
            previous = {}
        fields: StoredDeliverySlots = {}
        if "change_delivery" in previous:
            attempts = old_record(previous["change_delivery"]).copy()
            for intent in slots.intents():
                progress = old_record(attempts.get(intent.kind))
                if progress and progress["attempt"] != intent.attempt:
                    del attempts[intent.kind]
            fields["change_delivery"] = attempts
        if slots.waiting_why:
            fields["waiting_why"] = slots.waiting_why
        if isinstance(page, UserQuestionPage):
            fields["user_answer"] = page.saved
            if page.saved.get('kind') == 'question' and isinstance(slots.question, NoQuestion):
                slots = replace(slots, question=OpenQuestion(page.saved.get('question_id', ''),
                                                           page.saved.get('question_text', ''), NoDelivery()))
        if isinstance(page, ReviewMessagePage):
            fields["review_message"] = page.saved
        if isinstance(slots.validation, DeliveryIntent):
            fields["validation"] = {**stored_intent(slots.validation), "feedback": slots.validation.feedback}
        elif isinstance(slots.validation, ValidationRequested):
            fields["validation"] = {"attempt": slots.validation.attempt, "state": "sent",
                                    "feedback": slots.validation.feedback,
                                    "recipients": list(self.validators(page, row))}
        if isinstance(slots.verdict, DeliveryIntent):
            fields["verdict"] = stored_intent(slots.verdict)
        question = slots.question
        if isinstance(question, OpenQuestion):
            request = None
            if isinstance(question.delivery, DeliveryIntent):
                request = stored_intent(question.delivery)
                prior_request = question_intent_fields(previous)
                if (prior_request.get("attempt") == request["attempt"] and not question.delivery.recipients
                        and "recipients" not in prior_request):
                    _ = request.pop("recipients")
            fields["question"] = {"question_id": question.question_id, "text": question.text, "delivery": request}
        elif isinstance(question, AnsweredQuestion):
            fields["question"] = {"question_id": question.question_id, "text": question.text, "answer": question.answer}
        saved_question = fields.get("question")
        if saved_question is not None and isinstance(question, (OpenFeedback, CompletedFeedback)):
            saved_question["kind"] = "feedback"
        progress = question_progress or question_progress_for_slots(page, slots, previous)
        if progress is not None:
            fields["question_delivery"] = progress.stored()
        if fields and re.fullmatch(r"C\d+", row):
            if new_handoff:
                fields["handed_at"] = datetime.fromtimestamp(time.time(), user_zone()).isoformat()
            elif (saved := read_handed_at(path)) is not None:
                fields["handed_at"] = saved
        published = False
        try:
            if fields:
                resume = (change_review.clear_change_later(page.review_list, row)
                          if new_handoff and "handed_at" in fields else nullcontext())
                with resume:
                    try:
                        atomic_file.write_bytes_atomic(path, (json.dumps(fields) + "\n").encode("utf-8"), mode=0o600)
                    except atomic_file.AtomicDurabilityUnconfirmed:
                        published = True
                        raise
            else:
                path.unlink(missing_ok=True)
            published = True
        finally:
            if published:
                if progress is not None and progress.state.pending:
                    self.defer_question(path, UnreadableDeliveryRecord())
                intents = {intent.attempt for intent in slots.intents()}
                stale = [attempt for attempt, ownership in self.request_attempts.items()
                         if ownership.row_file == path and attempt not in intents]
                for attempt in stale:
                    del self.request_attempts[attempt]

    def begin(self, page: ReviewPage, row: str, kind: Literal["verdict", "validation"], feedback: str = "", *,
              recipients: tuple[staff.StaffId, ...] = (), waiting_why: str = "", user_verdict: bool = False) -> str:
        with self.locked():
            slots = self.slots(page, row)
            if user_verdict:
                require_user_turn(slots)
            return self.begin_locked(page, row, slots, kind, feedback, recipients=recipients, waiting_why=waiting_why)

    def begin_locked(self, page: ReviewPage, row: str, slots: DeliverySlots, kind: Literal["verdict", "validation"],
                     feedback: str = "", *, recipients: tuple[staff.StaffId, ...] = (), waiting_why: str = "") -> str:
        reason = delivery_state.require_waiting_why(waiting_why or feedback) if kind == "validation" else ""
        attempt = secrets.token_hex()
        intent = DeliveryIntent(attempt, kind, feedback if kind == "validation" else "", recipients)
        slots = slots.updated(kind, intent)
        self.write_slots(page, row, replace(slots, waiting_why=reason) if reason else slots, new_handoff=kind == "validation")
        self.request_attempts[attempt] = AttemptOwnership(self.path(page, row), "claimed")
        return attempt

    def record(self, delivery: MessageDelivery, result: FailedDelivery | NoFailedDelivery = NO_FAILED_DELIVERY,
               *, alerts: tuple[PendingDeliveryAlert, ...] = ()) -> None:
        with self.locked():
            _ = self.request_attempts.pop(delivery.attempt, None)
            page, row = delivery.page, delivery.row
            slots = self.slots(page, row)
            for current in slots.intents():
                if current.attempt == delivery.attempt:
                    self.record_result_locked(delivery, result, alerts)
                    self.finish_intent_locked(delivery, slots, current, delivered=isinstance(result, NoFailedDelivery))
                    return

    def finish_intent_locked(self, delivery: MessageDelivery, slots: DeliverySlots, intent: DeliveryIntent, *,
                             delivered: bool = True) -> None:
        page, row = delivery.page, delivery.row
        if intent.kind == "validation":
            updated = replace(slots, validation=ValidationRequested(intent.attempt, intent.feedback))
        else:
            updated = slots.updated(intent.kind, NoDelivery())
        if (delivered and intent.kind == "verdict" and isinstance(page, ReviewMessagePage)
                and page.saved.get("submission_request")):
            page = replace(page, saved={**page.saved, "submission_delivered": True})
        self.write_slots(page, row, updated)
        if is_change_question(delivery, slots, intent) or (
                intent.kind == "verdict" and isinstance(page, (ReviewMessagePage, UserQuestionPage))):
            self.remove_save_receipt(page, row, intent)

    def remove_save_receipt(self, page: ReviewPage, row: str, intent: DeliveryIntent) -> None:
        try:
            self.review_save_receipt(page, row, intent).unlink(missing_ok=True)
        except OSError as error:
            print(f"review-links delivery save receipt {row}: {error}", file=sys.stderr)

    def clear(self, page: ReviewPage, row: str, kind: Literal["verdict", "validation", "question"]) -> None:
        with self.locked():
            self.clear_locked(page, row, self.slots(page, row), kind)

    def clear_locked(self, page: ReviewPage, row: str, slots: DeliverySlots, kind: Literal["verdict", "validation", "question"]) -> None:
        updated = slots.updated(kind, NoDelivery())
        self.write_slots(page, row, updated)

    def ask(self, page: ReviewPage, row: str, text: str, *, recipients: tuple[staff.StaffId, ...] = (),
            kind: Literal["question", "feedback"] = "question") -> QuestionDelivery | NoDelivery:
        """Save a new question without changing an existing open question."""
        with self.locked():
            slots = self.slots(page, row)
            question = slots.question
            if isinstance(question, OpenQuestion):
                if question.text != text or isinstance(question, OpenFeedback) != (kind == "feedback"):
                    raise question_record.AnswerRefused("this row already has an open question")
                return NoDelivery()
            return self.ask_locked(page, row, slots, text, recipients, kind)

    def ask_locked(self, page: ReviewPage, row: str, slots: DeliverySlots, text: str,
                   recipients: tuple[staff.StaffId, ...], kind: Literal["question", "feedback"], *,
                   during_validation: bool = False, event: review_events.ReviewLog | None = None,
                   claim: bool = True) -> QuestionDelivery:
        reason = delivery_state.require_waiting_why(text)
        progress = source_question_progress(page.review_list, row, event) if event is not None else None
        intent = DeliveryIntent(progress.attempt if progress is not None else secrets.token_hex(),
                                "question", recipients=recipients)
        question_id = progress.question_id if progress is not None else secrets.token_hex(4)
        request = OpenFeedback if kind == "feedback" else OpenQuestion
        updated = replace(slots, question=request(question_id, text, intent))
        if not during_validation:
            updated = replace(updated, validation=NoDelivery(), waiting_why=reason)
        try:
            self.write_slots(page, row, updated, new_handoff=not during_validation, question_progress=progress)
        except Exception:
            if event is not None:
                self.question_sources_pending.set()
            raise
        if claim:
            self.request_attempts[intent.attempt] = AttemptOwnership(self.path(page, row), "claimed")
        return QuestionDelivery(question_id, text, intent.attempt)

    def answer_question(self, page: ReviewPage, row: str, question_id: str, text: str) -> bool:
        """Save an answer once and preserve the delivery record's other slots."""
        if not text.strip():
            raise question_record.AnswerRefused("enter an answer")
        text = text.strip()
        self.root.mkdir(parents=True, exist_ok=True)
        with question_answer_lock(self.path(page, row), question_id), self.locked():
            question = self.question_for_reply(page, row, question_id, text)
            if isinstance(question, AnsweredQuestion) and question.answer == text:
                return False
            if not isinstance(question, OpenQuestion):
                raise question_record.AnswerRefused("this row has no open question")
            record = answer_row(page, row)
            if record.get("Verdict"):
                raise question_record.AnswerRefused("this row already has a saved answer")
            production = row_production(page, record)
            if isinstance(question, OpenFeedback):
                words = "feedback: " + question.text
                events = review_events.decode(record.get("Review log", ""))
                if not any(event.words.rstrip().splitlines() == words.rstrip().splitlines() for event in events):
                    change_review.record_review_log(page.review_list, row, words, production)
            with change_review.clear_change_later(page.review_list, row):
                delivery_state.complete_review_reply(self.path(page, row), question_id, text)
            return True

    def question_for_reply(self, page: ReviewPage, row: str, question_id: str, text: str) -> OpenQuestion | AnsweredQuestion:
        """Apply the existing exchange identity rule to each Review type."""
        question = self.slots(page, row).question
        try:
            return delivery_state.question_exchange_for_reply(question, question_id, text)
        except delivery_state.StaleQuestionExchange as error:
            raise question_record.AnswerRefused(str(error)) from error

    def supersede_wait(self, page: ReviewPage, row: dict[str, str], outcome: str) -> str:
        """Remove the old Change wait before the new Review action saves."""
        with self.locked():
            slots = self.slots(page, row['#'])
            event = wait_supersession(page, row, outcome, slots)
            if event:
                change_review.record_review_log(page.review_list, row['#'], event, page.production)
                log_wait_supersession(page, row, event)
                self.write_slots(page, row['#'], replace(slots, validation=NoDelivery(), question=NoQuestion()))
            return event


def wait_supersession(page: ReviewPage, row: dict[str, str], outcome: str, slots: DeliverySlots) -> str:
    """Describe the wait that this action replaces."""
    if not slots.validation_requested and not isinstance(slots.question, OpenQuestion):
        return ''
    previous = ('question '+slots.question.question_id if isinstance(slots.question, OpenQuestion)
                else 'showrunner check')
    shown = change_review.display_id(row, route_owner(page, row).tab)
    return f'Review {shown}: {outcome} superseded the {previous} before the answer arrived.'


def log_wait_supersession(page: ReviewPage, row: dict[str, str], event: str, *, retry: bool = False,
                          destination: change_review.WaitSupersessionDestination | change_review.NoSavedWaitLogDestination
                          = change_review.NO_SAVED_WAIT_LOG_DESTINATION) -> None:
    if isinstance(destination, change_review.WaitSupersessionDestination):
        owner = staff.snapshot().by_id(destination.owner)
        if owner is None:
            raise Refusal(f'{destination.owner}: no showrunner staff row')
        production = destination.document.for_owner(owner)
    else:
        production = read_production(route_owner(page, row).tab, staff.snapshot())
    log = production.log
    if retry and log.exists():
        if any(line.endswith(': '+event) for line in log.read_text(encoding='utf-8').splitlines()):
            return
    update_registration.append_log(production, event, None, False)


@dataclass(frozen=True)
class DeliveryReadView:
    """One request scans record names once and reads only existing records."""

    status: DeliveryStatus
    paths: frozenset[Path]

    def slots(self, page: ReviewPage, row: str) -> DeliverySlots:
        if not self.paths or self.status.path(page, row) not in self.paths:
            return DeliverySlots(NoDelivery(), NoDelivery())
        return self.status.slots(page, row)


def script_version(script: str) -> str:
    """Name a page script by its content; an open page reloads, not updates, when the server's script differs."""
    return hashlib.sha256(script.encode()).hexdigest()[:12]


CHANGE_IMAGE_STYLE = """main:not(:has(>.page-status)):has(.change-image){max-width:1056px}
:is(section[data-type="changes"],section[data-change-card]){max-width:680px}:is(section[data-type="changes"],section[data-change-card]):has(>.change-image){max-width:992px;display:grid;grid-template-columns:minmax(0,680px) 280px;gap:32px;align-items:start}
.change-text{min-width:0}.change-image{display:block;width:280px;max-width:100%}
.change-image img{display:block;box-sizing:border-box;width:280px;max-width:100%;max-height:420px;object-fit:cover;object-position:top left;border:1px solid #8886;border-radius:6px}
[data-showrunner-filter]{max-width:680px}
@media(max-width:999px){:is(section[data-type="changes"],section[data-change-card]):has(>.change-image){display:block;max-width:680px}.change-image{margin-top:24px}}
"""


def change_content(row: dict[str, str], content: str) -> str:
    """Place the change text beside its thumbnail when its image exists."""
    path = change_review.image_path(row.get(change_review.IMAGE, ""))
    if not path:
        return content
    alt = html.escape("Screenshot: " + row.get("Change", "Change preview"), quote=True)
    thumbnail = (f'<a data-image-zoom class="change-image" href="{path}" target="_blank" rel="noopener">'
                 + f'<img src="{path}" alt="{alt}"></a>')
    return '<div class="change-text">' + content + '</div>' + thumbnail


def document(content: str, title: str = TITLES["changes"]) -> bytes:
    return ('''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>''' + html.escape(title) + '''</title><style>
:root{color-scheme:light dark;font:17px/1.55 system-ui,sans-serif}
body{margin:0}''' + console_theme.TOKENS + page_frame.STYLE + page_frame.ZOOM_STYLE + '''main{max-width:744px}
''' + CHANGE_IMAGE_STYLE + '''h1{font-size:26px;line-height:1.25;margin:0 0 16px}section h1,h2{font-size:20px;line-height:1.3}
h2{margin:16px 0}p{white-space:pre-wrap;margin:16px 0}main{overflow-wrap:anywhere}
section h2{font-size:17px}section details{margin:16px 0}
.answer-actions{display:flex;flex-wrap:wrap;gap:8px}.answer-actions button{margin:0}
@media (max-width:480px){.answer-actions{gap:6px}.answer-actions button{font-size:15px;padding:6px 8px}}
.change-details{display:grid;grid-template-columns:max-content 1fr;column-gap:1ch;margin:16px 0 0;font:14px/1.45 ui-monospace,monospace;opacity:.8}
.change-details dd{margin:0}
ul,ol{margin:16px 0;padding-left:28px;overflow-wrap:anywhere}
pre{white-space:pre-wrap;overflow-wrap:anywhere;border:1px solid #8886;border-radius:6px;padding:12px;font:14px/1.45 ui-monospace,monospace}
section+section{border-top:1px solid #8886;margin-top:36px;padding-top:28px}
form{margin-top:16px}label,textarea{display:block}textarea{box-sizing:border-box;width:100%;font:inherit;margin:8px 0 16px;resize:vertical}
button{font:inherit;min-height:44px;max-width:100%;margin:0 8px 8px 0;padding:6px 12px;background:light-dark(#f2f2f2,#303030);color:CanvasText;border:1px solid light-dark(#707070,#a0a0a0)}button[aria-pressed=true]{outline:2px solid currentColor}
summary{min-height:44px;align-content:center;cursor:pointer}button:focus-visible,summary:focus-visible,textarea:focus-visible,select:focus-visible{outline:2px solid currentColor;outline-offset:2px}
</style><style>''' + page_frame.COPY_STYLE + '''</style></head><body><main>''' + content + "</main><script>" + page_frame.ZOOM_SCRIPT + page_frame.COPY_SCRIPT + "</script></body></html>").encode("utf-8")


def answer_notice(page: ReviewPage, body: str, reason: str) -> AnswerNotice:
    fields = parse_qs(body, keep_blank_values=True)
    row = fields.get("row", [""])[0]
    outcome = fields.get("outcome", [""])[0]
    if row not in page.rows:
        row = ""
    if outcome not in ("works", "not works", "validate", "feedback", "question"):
        outcome = ""
    return AnswerNotice(row, reason, outcome, fields.get("feedback", [""])[0])


def answer_form(page: ReviewPage, notice: AnswerNotice, feedback_rows: int) -> str:
    """Render an answer without reading rows or queue state."""
    row = notice.row if notice.row in page.rows else page.rows[0]
    row_id = html.escape(row)
    buttons: list[str] = []
    for value, label in (("works", "Works"), ("not works", "Does not work"),
                         ("validate", "Ask showrunner to check"), ("question", "Ask a question"), ("feedback", "Send feedback")):
        if value in ("validate", "question", "feedback") and isinstance(page, AssignedReviewPage) and isinstance(page.owner, change_routing.UnroutedChange):
            continue
        selected = 'aria-pressed="true" ' if notice.outcome == value else ""
        buttons.append(f'<button {selected}name="outcome" value="{value}">{label}</button>')
    return f'''<form method="post">
<input type="hidden" name="row" value="{row_id}">
<input type="hidden" name="token" value="{html.escape(page.token)}">
<label for="feedback-{row_id}">Comment, question or feedback</label>
<textarea id="feedback-{row_id}" name="feedback" rows="{feedback_rows}">
{html.escape(notice.feedback)}</textarea>
<div class="answer-actions">{"".join(buttons)}</div></form>'''


_COMMIT = re.compile(r"^[0-9a-f]{7,40}$")
_ADDED = re.compile(r"^(\d{4}-\d{2}-\d{2})(?: (\d{2}:\d{2}))?$")


@dataclass(frozen=True)
class ChangeDetails:
    """The facts shown under a change's buttons; an empty value is unknown."""
    change: str
    date: str
    time: str
    commit: str
    kind: str

    @classmethod
    def read(cls, row: dict[str, str], key: str) -> ChangeDetails:
        """Split the Merged cell `<date>[ <HH:MM>][ · <commit>]` into its parts."""
        parts = [part.strip() for part in row.get("Merged", "").split(" · ")]
        added = _ADDED.match(parts[0])
        commit = parts[-1] if len(parts) > 1 and _COMMIT.match(parts[-1]) else ""
        return cls(key, added[1] if added else "", (added[2] or "") if added else "", commit, row.get(change_review.KIND, ""))

    def lines(self) -> list[tuple[str, str]]:
        return [(label, value) for label, value in (("change", self.change), ("date", self.date), ("time", self.time),
                                                    ("commit", self.commit), ("kind", self.kind)) if value]


def change_details(row: dict[str, str], key: str) -> str:
    """Show the change's facts as aligned lines, always open."""
    return ('<dl class="change-details">'
            + "".join(f"<dt>{label}:</dt><dd>{html.escape(value)}</dd>" for label, value in ChangeDetails.read(row, key).lines())
            + "</dl>")


def source_name(members: staff.StaffSnapshot, owner: staff.StaffId) -> str:
    """Include the parent showrunner in a unit's source line."""
    member = members.by_id(owner)
    if member is None:
        return str(owner)
    parent = members.parent_of(member) if member.kind == "unit" else None
    return f"{parent.name} · {member.name}" if parent is not None else member.name


def fallback_answer(page: ReviewPage, notice: AnswerNotice) -> bytes:
    feedback_rows = conf.read("review_links").whole("feedback_rows")
    return document(f'<p role="alert">Not saved: {html.escape(notice.reason)}</p>' + answer_form(page, notice, feedback_rows))


LINK = re.compile(r"(https?://(?:[^\s<>\"'`]|`(?![^`\n]+`))*"
                  + r"(?:[^\s<>\"'`*.,;:!?)\]]|`(?![^`\n]+`)))")
CODE_SPAN = re.compile(r"`([^`\n]+)`")
# Read code spans before URLs and bold delimiters.
INLINE_MARKUP = re.compile(CODE_SPAN.pattern + "|(?P<url>" + LINK.pattern + r")|\*\*")
HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)(?:[ \t]+#+[ \t]*)?$")


def linked(text: str) -> str:
    """Escape text and make each http or https address a link that opens in the same tab, outside the console frame."""
    pieces = LINK.split(text)
    return "".join(f'<a href="{html.escape(piece, quote=True)}" target="_top">{html.escape(piece)}</a>'
                   if index % 2 else html.escape(piece) for index, piece in enumerate(pieces))


def inline_html(text: str) -> str:
    """Escape text and render bold text, code spans, and address links."""
    content: list[str] = []
    start = 0
    bold_start = -1
    bold_content_start = 0
    for part in INLINE_MARKUP.finditer(text):
        plain = text[start:part.start()]
        if "\n" in plain:
            bold_start = -1
        content.append(html.escape(plain))
        if part[1] is not None:
            content.append(f"<code>{html.escape(part[1])}</code>")
        elif part["url"] is not None:
            content.append(linked(part["url"]))
        else:
            before = text[part.start() - 1] if part.start() else ""
            after = text[part.end()] if part.end() < len(text) else ""
            literal = ((before.isalnum() or before in ("_", "/"))
                       and (after.isalnum() or after in ("_", "/")))
            opens = (bool(after) and not after.isspace() and after != "/"
                     and (not before or before.isspace() or before in ("\"", "'")
                          or unicodedata.category(before) in ("Ps", "Pi")))
            closes = (bool(before) and not before.isspace() and before != "/"
                      and (not after or after.isspace() or unicodedata.category(after).startswith("P")))
            if not literal and bold_start >= 0 and part.start() > bold_content_start and closes:
                content[bold_start] = "<strong>"
                content.append("</strong>")
                bold_start = -1
            else:
                if not literal and bold_start < 0 and opens:
                    bold_start = len(content)
                    bold_content_start = part.end()
                content.append("**")
        start = part.end()
    content.append(html.escape(text[start:]))
    return "".join(content)


def review_text_html(text: str, *, inline: bool = False) -> str:
    """Render text blocks and lists. Enable Markdown when inline formatting is on."""
    if inline:
        return page_frame.render_code_blocks(text, lambda part: review_prose_html(part, inline=True))
    return review_prose_html(text)


def review_prose_html(text: str, *, inline: bool = False) -> str:
    """Render text blocks and lists between code blocks."""
    escape = inline_html if inline else html.escape
    lines = text.splitlines()
    if not any(re.match(r"\s*(?:- |[0-9]+\. )", line) or inline and HEADING.match(line.lstrip()) for line in lines):
        return f"<p>{escape(text)}</p>"
    content: list[str] = []
    current = ""
    for line in lines:
        line = line.lstrip()
        heading = HEADING.match(line) if inline else None
        if heading is not None:
            if current:
                content.append(f"</{current}>")
                current = ""
            level = len(heading[1])
            content.append(f"<h{level}>{escape(heading[2])}</h{level}>")
            continue
        numbered = re.match(r"([0-9]+)\. (.*)", line)
        tag = "ul" if line.startswith("- ") else "ol" if numbered else "p" if line else ""
        if tag != current:
            if current:
                content.append(f"</{current}>")
            if tag:
                start = (numbered[1].lstrip("0") or "0") if numbered else "1"
                attribute = f' start="{start}"' if tag == "ol" and start != "1" else ""
                content.append(f"<{tag}{attribute}>")
        elif tag == "p":
            content.append("\n")
        if tag in ("ul", "ol"):
            item = numbered[2] if numbered else line[2:]
            content.append(f"<li>{escape(item)}</li>")
        elif tag:
            content.append(escape(line))
        current = tag
    if current:
        content.append(f"</{current}>")
    return "".join(content)


BLANK_EDGES = re.compile(r"\A(?:[ \t]*\r?\n)+|(?:\r?\n[ \t]*)+\Z")


def review_html(text: str) -> str:
    """Render review text as paragraphs, lists, and code blocks with copy controls."""
    return page_frame.render_code_blocks(text, lambda part: review_prose_html(BLANK_EDGES.sub("", part)))


def question_failure_html(failed: bool) -> str:
    """Show one line for a confirmed delivery failure."""
    return '<p role="status" data-question-delivery-failed>Question not delivered.</p>' if failed else ""


def review_question_failed(status: DeliveryStatus, page: ReviewPage, row: str,
                           question: NoQuestion | OpenQuestion | AnsweredQuestion) -> bool:
    if not isinstance(question, OpenQuestion) or isinstance(question, OpenFeedback):
        return False
    path = status.path(page, row)
    try:
        fields = checked_delivery_fields(path)
        delivery = fields.get("question_delivery")
        if delivery is None:
            return False
        progress = read_question_progress(delivery)
        return progress.question_id == question.question_id and progress.state is QuestionDeliveryState.FAILED
    except (OSError, ValueError, KeyError, TypeError):
        return False


def question_html(question: NoQuestion | OpenQuestion | AnsweredQuestion, *, failed: bool = False) -> str:
    if isinstance(question, NoQuestion):
        return ""
    feedback = isinstance(question, (OpenFeedback, CompletedFeedback))
    content = ('<h2>Feedback</h2>' if feedback else '<h2>Question</h2>') + review_html(question.text)
    if isinstance(question, AnsweredQuestion):
        content += review_html('Showrunner: ' + question.answer) if feedback else '<h2>Answer</h2>' + review_html(question.answer)
    elif not failed:
        content += '<p>The showrunner will act on this</p>' if feedback else '<p>The showrunner will answer this</p>'
    return content + question_failure_html(failed)


def page_at(root: Path, request_path: str) -> ReviewPage:
    """Resolve only a published page's private allowlist, never a requested file path."""
    match = re.fullmatch(r"/([a-z0-9]{6})/changes\.html", urlsplit(request_path).path)
    if match is None:
        raise FileNotFoundError("not a change page")
    folder = root / match[1]
    manifest = folder / MANIFEST
    if folder.is_symlink() or manifest.is_symlink():
        raise FileNotFoundError("not a regular page manifest")
    decoded = cast(object, json.loads(manifest.read_text(encoding="utf-8")))
    if not isinstance(decoded, dict):
        raise ValueError("invalid page manifest")
    values = cast(dict[str, object], decoded)
    review_list, production, rows, token = (values.get(key) for key in
                                          ("review_list", "production", "rows", "token"))
    if not (isinstance(review_list, str) and isinstance(production, str)
            and isinstance(token, str) and token and isinstance(rows, list)):
        raise ValueError("invalid page manifest")
    row_values = cast(list[object], rows)
    if not row_values or not all(isinstance(row, str) and re.fullmatch(r"C\d+", row) for row in row_values):
        raise ValueError("invalid page rows")
    return ReviewPage(Path(review_list), Path(production), tuple(cast(list[str], rows)), token)


def shown_rows(page: ReviewPage) -> list[dict[str, str]]:
    rows = {row["#"]: row for row in change_review.review_rows(page.review_list)}
    selected: list[dict[str, str]] = []
    for row_id in page.rows:
        row = rows.get(row_id, {})
        if row.get(change_review.KIND) not in change_review.REVIEW_KINDS:
            raise ValueError(f"{row_id}: classify with change_review.py set-kind before publishing")
        if not row.get("Change") or not row.get(change_review.CHECK):
            raise ValueError(f"{row_id}: needs what changed and how to check / proof")
        selected.append(row)
    return selected


def answer_rows(page: ReviewPage) -> list[dict[str, str]]:
    """Allow older unclassified rows when the browser supplied a signed row token."""
    found = {row["#"]: row for row in change_review.review_rows(page.review_list)}
    if any(row not in found for row in page.rows):
        raise question_record.AnswerRefused("this change is no longer on the review list")
    return [found[row] for row in page.rows]


def answer_row(page: ReviewPage, row_id: str) -> dict[str, str]:
    return next(row for row in answer_rows(page) if row["#"] == row_id)


def answer_status(verdict: str, slots: DeliverySlots) -> str:
    if verdict:
        return "Saved: " + verdict
    return "The showrunner will validate this" if slots.validation_requested else ""


def list_routing(snapshots: dict[Path, change_review.ReviewListSnapshot]) -> change_routing.StaffRouting:
    documents = change_routing.production_documents(dict(showrunners.timer_docs()),
        ((listing.stem.removeprefix(change_review.PREFIX), snapshot.text) for listing, snapshot in snapshots.items()))
    return change_routing.StaffRouting(staff.snapshot(), documents)


def render(page: ReviewPage, delivery: DeliveryStatus, notice: AnswerNotice = NO_ANSWER_NOTICE) -> bytes:
    feedback_rows = conf.read("review_links").whole("feedback_rows")
    sections: list[str] = []
    saved = False
    for row in shown_rows(page):
        row_id = html.escape(row["#"])
        verdict = row.get("Verdict", "")
        saved = saved or bool(verdict)
        slots = delivery.slots(page, row["#"])
        question = slots.question
        failed = review_question_failed(delivery, page, row["#"], question)
        warning = ""
        if notice.reason and notice.row == row["#"]:
            reason = WITH_SHOWRUNNER if not verdict and not slots.waits_for_user(False) else notice.reason
            warning = "Not saved: " + reason
        alert = f'<p role="alert">{html.escape(warning)}</p>' if warning else ""
        form_notice = notice if notice.row == row["#"] else AnswerNotice(row["#"])
        status_text = answer_status(verdict, slots)
        answer = (f'<p role="status">{html.escape(status_text)}</p>' if status_text else
                  "" if isinstance(question, OpenQuestion) else answer_form(page, form_notice, feedback_rows))
        proof_title = "Try it · What you should see" if row[change_review.KIND] == "See in action" else "Proof"
        content = f'''{alert}
<h1>{html.escape(row["Change"])}</h1>
<h2>{proof_title}</h2>{review_html(row[change_review.CHECK])}
{question_html(question, failed=failed)}{answer}
{change_details(row, change_review.display_id(row, route_owner(page, row).tab))}'''
        sections.append(f'<section id="{row_id}" data-change-card>' + change_content(row, content) + '</section>')
    if notice.reason and not notice.row:
        sections.insert(0, f'<p role="alert">Not saved: {html.escape(notice.reason)}</p>')
    queue = next_item.change_queue(page.production)
    moved = urgency.ended(queue.name)
    if saved or any(queue_key(page, row) in moved for row in page.rows):
        if queue.entries:
            sections.append(f'''<form method="post">
<input type="hidden" name="token" value="{html.escape(page.token)}">
<button name="outcome" value="next">Skip for now</button></form>''')
        else:
            sections.append('<p>No changes to review.</p>')
    return document("".join(sections))


def queue_key(page: ReviewPage, row: str) -> str:
    """Keep the current list's row IDs and qualify rows from an earlier list."""
    if page.review_list.resolve() == change_review.scope_list(page.production).resolve():
        return row
    name = page.review_list.stem.removeprefix(change_review.PREFIX)
    return f"{name}/{row}"


def publish_next(page: ReviewPage, body: str) -> str:
    fields = parse_qs(body, keep_blank_values=True, strict_parsing=True)
    if (set(fields) != {"token", "outcome"} or fields.get("outcome") != ["next"]
            or len(fields["token"]) != 1
            or not secrets.compare_digest(fields["token"][0].encode("utf-8"), page.token.encode("utf-8"))):
        raise question_record.AnswerRefused("use the Next control on this page")
    queue = next_item.change_queue(page.production)
    moved = urgency.ended(queue.name)
    if not any(row.get("Verdict") or queue_key(page, row["#"]) in moved for row in shown_rows(page)):
        raise question_record.AnswerRefused("answer a change first")
    ordered = urgency.queue_order(queue.entries, queue.name)
    if not ordered:
        raise question_record.AnswerRefused("no changes remain")
    name, separator, row = ordered[0].key.rpartition("/")
    review_list = change_review.list_file(name) if separator else change_review.scope_list(page.production)
    return publish(page.production, review_list, [row])


def publish(production: Path, review_list: Path, rows: list[str]) -> str:
    listing = change_review.review_list_source(review_list)
    page = ReviewPage(listing, production.resolve(), tuple(dict.fromkeys(rows)), secrets.token_urlsafe())
    if not page.rows or any(re.fullmatch(r"C\d+", row) is None for row in page.rows):
        raise ValueError("name one or more row IDs such as C27")
    _ = shown_rows(page)
    _ = showrunner(page)
    manifest: PageManifest = {"review_list": str(page.review_list), "production": str(page.production),
                              "rows": list(page.rows), "token": page.token}
    # A fresh page has its own immutable row allowlist. Only metadata is stored
    # beside the HTML: every GET reads the answers from the review record.
    root = review_link.review_root()
    with build_folders.tool_folder("review-changes") as temporary:
        source = Path(temporary) / PAGE_NAME
        _ = source.write_text("<!doctype html><title>Changes to review</title>", encoding="utf-8")
        url = review_link.snapshot_at(root, [source], new=True)[0]
    folder = root / urlsplit(url).path.split("/")[1]
    atomic_file.write_bytes_atomic(folder / MANIFEST, json.dumps(manifest).encode("utf-8"), mode=0o600)
    return url


def showrunner(page: ReviewPage) -> str:
    """The record names the recipient, including when its production is offline."""
    if isinstance(page, AssignedReviewPage):
        return page.owner.tab
    text = change_review.read(page.review_list)
    frontmatter = text.split("---", 2)
    if len(frontmatter) == 3:
        for line in frontmatter[1].splitlines():
            if line.startswith("showrunner:") and (name := line.partition(":")[2].strip()):
                return name
    raise ValueError("review list has no showrunner in its frontmatter")


def row_production(page: ReviewPage, row: dict[str, str]) -> Path:
    """Resolve the row's Production cell for the existing feature-log closeout rule."""
    record = staff.snapshot()
    if not row.get("Verdict"):
        assignment = change_routing.StaffRouting(record, {}).owner_and_production(page.review_list, row)
        if isinstance(assignment.production, Path):
            return assignment.production
        raise question_record.AnswerRefused(assignment.production.reason)
    root = change_review.showrunner_of(page.production, record=record)
    owner = row.get(change_review.PRODUCTION) or root
    found = showrunners.owner_for(owner, record)
    if isinstance(found.doc, (staff.LiveDoc, staff.WrappedDoc)):
        return found.doc.path
    raise question_record.AnswerRefused(f"unknown production {owner} for {row['#']}")


def route_owner(page: ReviewPage, row: dict[str, str]) -> change_routing.ChangeOwner:
    """Resolve a legacy selected-row page through the shared staff router."""
    if isinstance(page, AssignedReviewPage):
        return page.owner
    routing = list_routing({page.review_list: change_review.review_snapshot(page.review_list)})
    return routing.owner_and_production(page.review_list, row).owner


@overload
def send_to_staff(identity: staff.StaffId, sender: str, message: str) -> None: ...


@overload
def send_to_staff(identity: staff.StaffId, sender: str, message: str, *, key: str, question: bool = False
                  ) -> Literal["skipped", "pending"] | None: ...


def send_to_staff(identity: staff.StaffId, sender: str, message: str, *, key: str = "", question: bool = False
                  ) -> Literal["skipped", "pending"] | None:
    """Resolve the current conversation immediately before each send."""
    member = question_recipient(identity) if key or question else staff.snapshot().by_id(identity)
    if member is None:
        raise RuntimeError(f"no staff record for {identity}")
    address = f"session:{member.current_session}"
    if key:
        return send_message(address, sender, message, key=key, recipient_key=identity, question_kind=member.kind)
    _ = send_message(address, sender, message)
    return None


def review_send_options(key: str, recipient_key: str) -> tuple[str, ...]:
    """Use the saved attempt and staff ID for every copy of this send."""
    return ("--review-delivery", "--key", key, "--recipient-key", recipient_key,
            "--repeat-minutes", "52560000")


def mark_sender_started() -> None:
    marker = _transport_started.get()
    if marker is None:
        return
    _ = _transport_started.set(None)
    try:
        marker()
    except OSError as error:
        print(f"review-links send marker: {error}", file=sys.stderr)


def question_transport(recipient: staff.StaffId, kind: staff.Kind, sender: str, message: str, key: str
                       ) -> tuple[send_result.ConfirmedSendOutcome | send_result.UnconfirmedSendOutcome, str]:
    """Send to the current staff member without adding a deferred copy."""
    # A fixed staff lookup disables the socket queue. Urgent bypasses the review hold.
    try:
        timeout = send_budget.seconds()
    except (OSError, conf.Unusable) as error:
        raise QuestionSendStopped(str(error)) from error
    options = (*review_send_options(key, recipient), "--recipient-kind", kind, "--urgent")
    claim = _transport_claim.get()
    try:
        deadline = _question_transport_deadline.get()
        if deadline is not None and time.time() >= deadline:
            raise QuestionRecoveryEnded(QUESTION_RECOVERY_EXPIRED)
        return unrouted_alert.message_outcome(recipient, sender, message, *options, timeout=timeout,
                                              claim_fd=claim, started=mark_sender_started)
    except unrouted_alert.SenderNotStarted as error:
        raise QuestionNotStarted(str(error)) from error


class UnconfirmedSend(RuntimeError):
    """The sender could not confirm whether the recipient accepted the message."""


def send_message(recipient: str, sender: str, message: str, *, key: str = "",
                 recipient_key: str = "", question_kind: staff.Kind | None = None
                 ) -> Literal["skipped", "pending"] | None:
    if not key:
        unrouted_alert.send_message(recipient, sender, message)
        return None
    if question_kind is None:
        raise QuestionNotStarted("The recipient kind is unavailable.")
    outcome, error = question_transport(staff.StaffId(recipient_key), question_kind, sender, message, key)
    receipt = question_send_result(outcome, error)
    return "skipped" if receipt.outcome == "kept" else None


def feature_review_id(page: ReviewPage, doc: Path, row_id: str) -> str:
    """Find the feature-log proposal for a Change row."""
    lines = doc.read_text(encoding="utf-8").splitlines()
    repo = Path(production_field(lines, "Showrunner checkout")).expanduser()
    project = feature_log.project_of(str(repo))
    if project not in {name for name, spec in feature_log.PROJECTS.items() if spec.closes_by_change_review}:
        return ""
    list_name = page.review_list.stem.removeprefix(change_review.PREFIX)
    return row_id if list_name == "hana" else f"{list_name}/{row_id}"


def close_feature_log(review: str, verdict: str, *, check_only: bool = False) -> list[str]:
    """Check or apply the closeout for the feature proposal."""
    try:
        if change_review.lands(verdict):
            return feature_log.land_review(review, if_proposed=True, check_only=check_only)
        return feature_log.drop_review(review, verdict, if_proposed=True, check_only=check_only)
    except feature_log.ProposalCloseoutBlocked:
        raise
    except feature_log.FeatureLogError as error:
        raise RuntimeError(error.diagnostic) from error


def closeout_before_save(page: ReviewPage, doc: Path, row_id: str, verdict: str, status: DeliveryStatus) -> str:
    """Refuse the verdict if the feature proposal cannot accept it."""
    try:
        review = feature_review_id(page, doc, row_id)
        if review and close_feature_log(review, verdict, check_only=True) != [f"skipped: no proposal for {review}"]:
            return review
        return ''
    except (*SEND_ERRORS, feature_log.ProposalCloseoutBlocked) as error:
        reason = error.diagnostic if isinstance(error, feature_log.ProposalCloseoutBlocked) else str(error)
        message = f"Change review {row_id} stays open: feature log failed.\n{reason}\nList: {page.review_list}"
        try:
            row = answer_row(page, row_id)
            owner = route_owner(page, row)
            status.notify_closeout_failure(page, row_id, message, owner.showrunner_recipients)
        except SEND_ERRORS as delivery_error:
            raise FeatureLogFailed(f"{message}\nNotification failed: {delivery_error}") from delivery_error
        raise FeatureLogFailed(message) from error


def submit(page: ReviewPage, body: str, status: DeliveryStatus, *,
           prepared: change_review.ChangeQueue | next_item.ReadChangeLists = next_item.READ_CHANGE_LISTS,
           feedback_closes: bool = False, defer_verdict_cleanup: bool = False) -> MessageDelivery | NoMessageDelivery:
    del feedback_closes
    fields = parse_qs(body, keep_blank_values=True, strict_parsing=True)
    if set(fields) != {"row", "token", "outcome", "feedback"} or any(len(values) != 1 for values in fields.values()):
        raise question_record.AnswerRefused("expected only row, token, outcome and feedback")
    row_id, token, outcome, feedback = (fields[key][0] for key in ("row", "token", "outcome", "feedback"))
    feedback = "\n".join(feedback.splitlines())
    if row_id not in page.rows or not secrets.compare_digest(token.encode("utf-8"), page.token.encode("utf-8")):
        raise question_record.AnswerRefused("row or page token does not match this page")
    during_validation = outcome == VALIDATION_QUESTION and defer_verdict_cleanup
    if during_validation:
        outcome = "question"
    if outcome not in ("works", "not works", "validate", "feedback", "question"):
        raise question_record.AnswerRefused("choose an answer")
    one_line = " ".join(line.strip() for line in feedback.split("\n") if line.strip())
    verdict = one_line or outcome
    if outcome in ("works", "not works") and one_line:
        verdict = f"{outcome}: {one_line}"
    rows = answer_rows(page) if isinstance(page, AssignedReviewPage) else shown_rows(page)
    row = next(row for row in rows if row["#"] == row_id)
    if isinstance(page, AssignedReviewPage):
        assignment = change_routing.ChangeAssignment(page.owner, page.production)
    else:
        routing = list_routing({page.review_list: change_review.review_snapshot(page.review_list)})
        assignment = routing.owner_and_production(page.review_list, row)
    owner = assignment.owner
    if isinstance(assignment.production, change_routing.UnavailableProduction):
        raise question_record.AnswerRefused("the production document is not available")
    message_page = replace(page, production=assignment.production)
    recipients = owner.showrunner_recipients
    if outcome in CHANGE_HANDOFFS:
        with status.locked():
            if answer_row(page, row_id).get("Verdict"):
                raise question_record.AnswerRefused("this row already has a saved answer")
            slots = status.slots(page, row_id)
            if during_validation:
                recipients = status.validators(page, row_id)
                if isinstance(slots.question, OpenQuestion):
                    if slots.validation_requested and not isinstance(slots.question, OpenFeedback) and slots.question.text == feedback:
                        return NoMessageDelivery()
                    raise question_record.AnswerRefused("this row already has an open question")
                if not slots.validation_requested:
                    raise ChangeHandoffRefused("This change is no longer with the validator.")
            else:
                require_user_turn(slots)
            if outcome in ("feedback", "question") and not feedback.strip():
                raise question_record.AnswerRefused("enter a question" if outcome == "question" else "enter feedback")
            if isinstance(slots.verdict, DeliveryIntent):
                raise question_record.AnswerRefused("A verdict for this change is being saved.")
            if not during_validation and isinstance(owner, change_routing.UnroutedChange) and outcome != "question":
                raise question_record.AnswerRefused("this row has no showrunner to receive the request")
            _ = row_production(page, row)
            if outcome == "validate":
                _ = next_item.move_to_end(next_item.CHANGE, page.production, key=queue_key(page, row_id), prepared=prepared)
                change_review.record_review_log(page.review_list, row_id,
                    "validate" + (": " + feedback if feedback else ""), message_page.production)
                attempt = status.begin_locked(page, row_id, slots, "validation", feedback, recipients=recipients,
                    waiting_why=feedback.strip() or "The user asked the showrunner to validate this change.")
                message = validation_message(message_page, row, attempt, feedback)
            else:
                request_feedback = outcome == "feedback"
                event = None
                if request_feedback:
                    change_review.record_review_log(page.review_list, row_id, "feedback: " + feedback, message_page.production)
                else:
                    try:
                        try:
                            change_review.record_review_log(page.review_list, row_id, "question: " + feedback,
                                                            message_page.production)
                        except atomic_file.AtomicDurabilityUnconfirmed as error:
                            print(f"review-links question source {row_id}: {error}", file=sys.stderr)
                        event = change_question_event(answer_row(page, row_id))
                        if event is None or question_words(event) != feedback:
                            raise OSError("The saved Change question does not match the submitted question.")
                    except Exception:
                        status.question_sources_pending.set()
                        raise
                intent = status.ask_locked(page, row_id, slots, feedback, recipients,
                                            "feedback" if request_feedback else "question",
                                            during_validation=during_validation, event=event)
                attempt = intent.attempt
                message = _request_message(message_page, row, intent.question_id, intent.text, request_feedback,
                                           sender=question_sender(row, recipients) if during_validation else None)
        return MessageDelivery(message_page, row_id, recipients, message, attempt, recipients)

    if row.get("Verdict"):
        if row["Verdict"] != verdict:
            raise question_record.AnswerRefused("this row already has a different saved answer")
        return NoMessageDelivery()
    doc = assignment.production if defer_verdict_cleanup else row_production(page, row)
    recipients = verdict_recipients(owner, verdict)
    attempt = ''
    with status.locked():
        slots = status.slots(page, row_id)
        require_user_turn(slots)
        if isinstance(slots.verdict, DeliveryIntent):
            raise AnswerRefused("A verdict for this change is being saved.")

    def cancel_preparation() -> None:
        with status.locked():
            if attempt and status.slots(page, row_id).verdict != slots.verdict:
                status.write_slots(page, row_id, slots)

    if recipients and not defer_verdict_cleanup:
        attempt = status.begin(page, row_id, "verdict", recipients=recipients, user_verdict=True)
    try:
        if recipients and defer_verdict_cleanup:
            attempt = status.begin(page, row_id, "verdict", recipients=recipients, user_verdict=True)
        review = closeout_before_save(page, doc, row_id, verdict, status)
        production_name = unit_lookup.doc_owner(page.production, staff.snapshot()).name
        production = change_review.production_doc(production_name)
        with change_review.checked_review_write(page.review_list, status.root, lambda: None):
            recorded = answer_row(page, row_id).get('Verdict')
            if recorded == verdict:
                cancel_preparation()
                return NoMessageDelivery()
            if recorded:
                raise AnswerRefused("this row already has a different saved answer")
            cleanup = replace(change_review.PendingVerdictCleanup.for_row(status.root, page.review_list, row_id),
                              verdict=verdict, feature_review=review, require_git_commit=defer_verdict_cleanup or bool(review))
            try:
                cleanup.save()
                result = change_review.save_verdict(page.review_list, row_id, verdict, production, cleanup=cleanup)
            except (change_review.ChangeReviewError, OSError):
                cleanup.intent.unlink(missing_ok=True)
                raise
            if isinstance(result, change_review.VerdictRefused) or isinstance(result.cleanup, change_review.NoVerdictCleanup):
                cleanup.intent.unlink(missing_ok=True)
    except change_review.VerdictDurabilityUnconfirmed:
        with status.locked():
            _ = status.request_attempts.pop(attempt, None)
        raise
    except (FeatureLogFailed, Refusal):
        cancel_preparation()
        raise
    except (change_review.ChangeReviewError, OSError) as error:
        cancel_preparation()
        if defer_verdict_cleanup:
            raise question_record.AnswerRefused("The answer could not be saved.") from error
        raise question_record.AnswerRefused(str(error).strip()) from error
    for warning in result.warnings:
        print(f"review-links {warning}", file=sys.stderr)
    if isinstance(result, change_review.VerdictRefused):
        for line in result.lines:
            if line.startswith("scratch not removed:"):
                print(f"review-links {line}", file=sys.stderr)
        cancel_preparation()
        raise question_record.AnswerRefused(result.reason)
    details = (f"Merged: {row.get('Merged', '')}", f"User feedback: {feedback if feedback.strip() else verdict}",
               "\n".join(result.lines).strip())
    try:
        message = review_message(message_page, row, verdict, *details)
    except Exception as error:
        print(f"review-links {row_id}: the verdict is saved. Message build failed: {error}", file=sys.stderr)
        message = verdict_message(message_page, row, change_review.display_id(row, owner.tab), production_name, verdict, *details)
    return (MessageDelivery(message_page, row_id, recipients, message, attempt, owner.showrunner_recipients, result.cleanup)
            if recipients else NoMessageDelivery(result.cleanup))


def clear_saved_verdict_wait(status: DeliveryStatus,
                             cleanup: change_review.PendingVerdictCleanup | change_review.NoVerdictCleanup) -> None:
    if not isinstance(cleanup, change_review.PendingVerdictCleanup) or not cleanup.superseded_wait:
        return
    page = ReviewPage(cleanup.path, cleanup.path, (cleanup.row,), "")
    with status.locked():
        slots = status.slots(page, cleanup.row)
        updated = replace(slots, validation=NoDelivery(), question=NoQuestion())
        if updated != slots:
            status.write_slots(page, cleanup.row, updated)


def finish_saved_verdict(cleanup: change_review.PendingVerdictCleanup, status: DeliveryStatus) -> list[str]:
    """Close the feature and remove scratch after the verdict is durable."""
    page = ReviewPage(cleanup.path, cleanup.path, (cleanup.row,), "")

    def closeout(current: change_review.PendingVerdictCleanup) -> None:
        if current.superseded_wait:
            log_wait_supersession(page, answer_row(page, current.row), current.superseded_wait, retry=True,
                                  destination=current.wait_log)
            clear_saved_verdict_wait(status, current)
        if not current.feature_review:
            return
        try:
            _ = close_feature_log(current.feature_review, current.verdict)
        except feature_log.ProposalCloseoutBlocked as error:
            replace(current, feature_review='').save()
            message = (f"Change review {current.row} is saved: feature log failed.\n"
                       f"{error.diagnostic}\nList: {current.path}")
            try:
                owner = route_owner(page, answer_row(page, current.row))
                status.notify_closeout_failure(page, current.row, message, owner.showrunner_recipients)
            except SEND_ERRORS as failure:
                print(f"review-links closeout alert {current.row}: {failure}", file=sys.stderr)

    return cleanup.finish(closeout)


def finish_verdict_cleanup(delivery: MessageDelivery | NoMessageDelivery,
                           status: DeliveryStatus) -> MessageDelivery | NoMessageDelivery:
    """Finish the saved verdict and include its scratch results in the message."""
    cleanup = delivery.scratch_cleanup
    if not isinstance(cleanup, change_review.PendingVerdictCleanup):
        return delivery
    lines = finish_saved_verdict(cleanup, status)
    for line in lines:
        if isinstance(delivery, NoMessageDelivery) or line.startswith("scratch not removed:"):
            print(f"review-links {line}", file=sys.stderr)
    if isinstance(delivery, MessageDelivery):
        return replace(delivery, message='\n'.join((delivery.message, *lines)), scratch_cleanup=change_review.NO_VERDICT_CLEANUP)
    return NoMessageDelivery()


def review_message(page: ReviewPage, row: dict[str, str], headline: str, *details: str) -> str:
    shown, production = change_message_identity(page, row)
    return verdict_message(page, row, shown, production, headline, *details)


def change_message_identity(page: ReviewPage, row: dict[str, str]) -> tuple[str, str]:
    return (change_review.display_id(row, route_owner(page, row).tab),
            unit_lookup.doc_owner(page.production, staff.snapshot()).name)


def verdict_message(page: ReviewPage, row: dict[str, str], shown: str, production: str, headline: str, *details: str) -> str:
    return "\n".join((f"Change review {shown}: {headline}", f"List: {page.review_list}",
                      f"Production: {production}", f"Change: {row['Change']}", *details))


def validation_message(page: ReviewPage, row: dict[str, str], identity: str, feedback: str, *,
                       sender: tuple[str, str] | None = None) -> str:
    """Include the current validation ID in the native answer command."""
    shown, owner = change_message_identity(page, row) if sender is None else sender
    command = ('~/.claude/scripts/lib/py ~/.claude/scripts/production/change_review.py verdict'
               + ' --production '+shlex.quote(owner)+' --list '+shlex.quote(str(page.review_list))
               + ' --row '+shlex.quote(row['#'])+' --validation '+shlex.quote(identity)
               + " --verdict '<works or problem>'")
    return verdict_message(page, row, shown, owner, 'the user asks you to validate this', f'Validation id: {identity}',
                           f'User feedback: {feedback}', 'Answer with this command:\n'+command)


def question_sender(row: dict[str, str],
                    recipients: tuple[staff.StaffId, ...]) -> tuple[str, str]:
    validator = staff.snapshot().by_id(recipients[0]) if recipients else None
    name = validator.name if validator is not None else "Validator unavailable"
    return change_review.display_id(row, name), name


def _request_message(page: ReviewPage, row: dict[str, str], identity: str, text: str, feedback: bool, *,
                     sender: tuple[str, str] | None = None) -> str:
    label = "Feedback" if feedback else "Question"
    shown, owner = change_message_identity(page, row) if sender is None else sender
    return verdict_message(page, row, shown, owner, "the user sends feedback to act on" if feedback else "the user asks a question",
                           f"{label} id: {identity}", f"{label}: {text}",
                           'Answer with this command:\n'+reply_command(
                               page.review_list.stem.removeprefix(change_review.PREFIX)+'/'+row['#'],identity))


def verdict_recipients(owner: change_routing.ChangeOwner, verdict: str) -> tuple[staff.StaffId, ...]:
    """Use the same staff recipients for new and legacy verdict attempts."""
    if verdict != "works" and isinstance(owner, change_routing.StaffRoutedChange):
        return tuple(dict.fromkeys((owner.unit_id, *owner.showrunner_recipients)))
    return owner.showrunner_recipients


@dataclass(frozen=True)
class MissingRecipient:
    """No staff recipient exists for this message."""


@dataclass(frozen=True)
class DeliveryFailure:
    """Keep one failed recipient and its last send error."""

    recipient: staff.StaffId | MissingRecipient
    error: str


def question_intent_fields(fields: dict[str, object]) -> dict[str, object]:
    exchange = record_field(fields, "question")
    return record_field(exchange, "delivery") if exchange.get("delivery") is not None else {}


def source_question_key(source: str, row: str, time_text: str, words: str) -> str:
    """Derive the same delivery ID from the saved question on every read."""
    return hashlib.sha256(json.dumps((source, row, time_text, words)).encode()).hexdigest()


def source_question_progress(path: Path, row: str, event: review_events.ReviewLog,
                             question_id: str = "") -> QuestionProgress:
    attempt = source_question_key(str(path.resolve()), row, event.time.isoformat(), event.words)
    return QuestionProgress(attempt, question_id or attempt[:8], event.time.timestamp())


def latest_question_event(events: tuple[review_events.ReviewLog, ...]) -> review_events.ReviewLog | None:
    """Find the last Ask unless a later action ends its turn."""
    for event in reversed(checked_review_events(events)):
        label = event.words.partition(":")[0].lower()
        if label in ("question", "followup"):
            return event
        if label == "user chose question" or label.startswith("user chose question "):
            continue
        if label in ("answer", "validate", "feedback", "feedback sent", "approve", "decline", "marked read",
                     "superseded followup") or label.startswith("user chose "):
            break
    return None


def change_question_event(row: dict[str, str]) -> review_events.ReviewLog | None:
    return latest_question_event(read_recovery_log(row.get("Review log", "")))


def read_recovery_log(text: str) -> tuple[review_events.ReviewLog, ...]:
    if not text:
        return ()
    raw = cast(object, json.loads(text))
    if not isinstance(raw, list):
        raise ValueError("Invalid saved Review log.")
    for value in cast(list[object], raw):
        row = require_record(value, "Review log entry")
        check_text_fields(row, (), required=("time", "who", "words"))
    return review_events.from_rows(cast(list[review_events.StoredReviewLog], raw))


def checked_review_events(events: tuple[review_events.ReviewLog, ...]) -> tuple[review_events.ReviewLog, ...]:
    if any(not isinstance(cast(object, value), str) for event in events for value in (event.who, event.words)):
        raise ValueError("Invalid saved Review log entry.")
    return events


def question_words(event: review_events.ReviewLog) -> str:
    return event.words.partition(": ")[2]


def needs_source_question(fields: dict[str, object], progress: QuestionProgress, text: str) -> bool:
    """Preserve delivery evidence for the source question and for older records."""
    if "question_delivery" in fields:
        current = read_question_progress(fields["question_delivery"])
        return current.attempt != progress.attempt and current.saved_at < progress.saved_at
    slots = decode_change_delivery(fields)
    if isinstance(slots.question, (OpenQuestion, AnsweredQuestion)):
        return isinstance(slots.question, (OpenFeedback, CompletedFeedback)) or slots.question.text != text
    return True


def review_delivery_key(source: Path, row: str, attempt: str, *, purpose: str = "message") -> str:
    """Identify one send across retries and server restarts."""
    identity = json.dumps((str(source.resolve()), row, attempt, purpose))
    return "review:" + hashlib.sha256(identity.encode()).hexdigest()


def deliver_page_feedback(path: Path, key: str, recipient: staff.StaffId, message: str, *,
                          sender: str = SENDER) -> QuestionProgress:
    """Keep a receipt for one saved List or Images action."""
    progress = QuestionProgress(key, key, time.time())
    with sender_claim(path.with_suffix(".lock")) as claimed:
        if not claimed:
            return progress
        try:
            progress = read_question_progress(read_delivery_object(path))
            if progress.attempt != key:
                raise ValueError("The feedback receipt has a different key.")
        except FileNotFoundError:
            progress, error = progress.start_budget()
            if error:
                progress = progress.fail(error)
            write_feedback_progress(path, progress)
        result = progress.expire()
        if result.state is QuestionDeliveryState.NOT_STARTED:
            result, _ = attempt_question_send(progress, partial(send_review_question, recipient, message, key,
                                                                  sender=sender),
                started=lambda: write_feedback_progress(path, begin_question_send(progress)))
        if result != progress:
            write_feedback_progress(path, result)
        return result


def write_feedback_progress(path: Path, progress: QuestionProgress) -> None:
    atomic_file.write_text_atomic(path, json.dumps(progress.stored()) + "\n", new_file_mode=0o600)


def staff_showrunner(owner: staff.StaffId, members: staff.StaffSnapshot) -> tuple[staff.StaffRow, ...]:
    """Find the first showrunner on the owner's staff path."""
    member = members.by_id(owner)
    visited: set[staff.StaffId] = set()
    while member is not None and member.id not in visited:
        if member.kind == "showrunner":
            return (member,)
        visited.add(member.id)
        member = members.parent_of(member)
    return ()


def review_alert_showrunners(owner: staff.StaffId) -> tuple[staff.StaffId, ...]:
    """Find the showrunner for the Review item's owner."""
    members = staff.snapshot()
    if runners := staff_showrunner(owner, members):
        return tuple(runner.id for runner in runners)
    fallback = conf.read("review_links").text("unrouted_alert_recipient")
    try:
        return (showrunners.owner_for(fallback, members).id,)
    except ValueError:
        return ()


def is_change_question(delivery: MessageDelivery, slots: DeliverySlots, intent: DeliveryIntent) -> bool:
    return (intent.kind == "question" and not isinstance(slots.question, OpenFeedback)
            and re.fullmatch(r"C\d+", delivery.row) is not None)


def review_open_question(page: ReviewPage, slots: DeliverySlots, intent: DeliveryIntent) -> OpenQuestion | NoQuestion:
    question = slots.question
    if not isinstance(question, OpenQuestion) or isinstance(question, OpenFeedback):
        return NoQuestion()
    if question.delivery == intent or native_review_question(page, slots, intent):
        return question
    return NoQuestion()


def native_review_question(page: ReviewPage, slots: DeliverySlots, intent: DeliveryIntent) -> bool:
    """Identify a native question stored in the verdict slot, including after an answer."""
    if intent.kind != "verdict":
        return False
    if isinstance(page, UserQuestionPage):
        return page.saved.get("kind") == "question"
    return (isinstance(page, ReviewMessagePage) and not page.saved.get("submission_request")
            and isinstance(slots.question, (OpenQuestion, AnsweredQuestion))
            and not isinstance(slots.question, (OpenFeedback, CompletedFeedback)))


def saved_user_question(page: UserQuestionPage) -> update_registration.QuestionRecord:
    records = update_registration.selected_question_records(
        [page.review_list], (), None, owner=page.saved["owner"], strict=True)
    if len(records) != 1:
        raise ValueError("Cannot read the saved user question.")
    return records[0]


def open_user_question(page: UserQuestionPage) -> update_registration.QuestionRecord | None:
    current = saved_user_question(page)
    return current if current.status == "open" else None


def read_recovery_proposal(path: Path) -> Issue:
    fields = record_store.read_header(path, strict=True)
    if not fields:
        raise ValueError("Cannot read the saved Proposal.")
    return Issue(path, fields)


def user_question_event(record: update_registration.QuestionRecord) -> review_events.ReviewLog | None:
    followup = record.item.get("followup")
    if followup is None:
        return None
    event = latest_question_event(read_recovery_log(record.item.get("review_log", "")))
    if event is None or review_events.require_waiting_why(question_words(event)) != (
            review_events.require_waiting_why(followup["text"])):
        return None
    return event


def native_question_event(page: ReviewMessagePage | UserQuestionPage, row: str) -> review_events.ReviewLog | None:
    """Read the Ask event for the current native Review question."""
    path = page.review_list
    if isinstance(page, UserQuestionPage):
        current = open_user_question(page)
        return user_question_event(current) if current is not None else None
    if path.parent.name in ("investigations", "archive") and re.fullmatch(r"I\d+", row):
        item = investigation.read_review_record(path)
        if item.id == row and item.state == investigation.InvestigationState.OPEN:
            return latest_question_event(item.log)
    elif path.name.endswith(".review.json"):
        from scripts.review_links.efficiency_reviews import decode

        reviews = delivery_state.read_fields(path)
        if row in reviews:
            review = decode(reviews[row])
            if review.mark == review_events.ReviewMark.WAITING:
                return latest_question_event(review.log)
    else:
        issue = read_recovery_proposal(path)
        if not proposal_issue.review_is_done(issue) and proposal_issue.review_mark(issue) == review_events.ReviewMark.WAITING:
            return latest_question_event(proposal_issue.review_log(issue))
    return None


def native_answer_log(page: ReviewMessagePage | UserQuestionPage, row: str
                      ) -> tuple[tuple[review_events.ReviewLog, ...], str, str]:
    """Read the log, current reply token, and legacy reply from a native source."""
    path = page.review_list
    if isinstance(page, UserQuestionPage):
        item = saved_user_question(page).item
        details = item.get("review_submission")
        submission = new_item_dialog.read_submission(details) if details else None
        exchanges = item.get("followups", [])
        reply = exchanges[-1]["reply"] if (page.saved.get("legacy_followup") and exchanges
            and "followup" not in item and exchanges[-1]["followup"] in page.saved["message"]) else ""
        return read_recovery_log(item.get("review_log", "")), submission.reply_token if submission else "", reply
    if path.parent.name in ("investigations", "archive") and re.fullmatch(r"I\d+", row):
        item = investigation.read_review_record(path)
        if item.id != row:
            return (), "", ""
        origin = item.submission
        return item.log, origin.reply_token if isinstance(origin, new_item_dialog.ReviewSubmission) else "", ""
    if path.name.endswith(".review.json"):
        from scripts.review_links.efficiency_reviews import decode

        saved = delivery_state.read_fields(path)
        return decode(saved[row]).log if row in saved else (), "", ""
    issue = read_recovery_proposal(path)
    origin = proposal_issue.review_submission(issue)
    return proposal_issue.review_log(issue), origin.reply_token if isinstance(origin, new_item_dialog.ReviewSubmission) else "", ""


def native_source_answer(page: ReviewMessagePage | UserQuestionPage, row: str, question: OpenQuestion,
                         progress: QuestionProgress) -> str:
    """Match the answer to its Ask before changing the delivery."""
    try:
        _ = page.review_list.stat()
        entries, token, legacy = native_answer_log(page, row)
    except FileNotFoundError:
        return ""
    if token and token != question.question_id:
        return ""
    answer = ""
    expected = page.saved.get("question_source_key")
    for event in reversed(checked_review_events(entries)):
        label, _, words = event.words.partition(": ")
        if label.casefold() in ("question", "followup"):
            proof = source_question_progress(page.review_list, row, event)
            if expected:
                return answer if proof.attempt == expected else ""
            matches = question.text.strip() == words.strip()
            return answer if matches and (token or event.time.timestamp() <= progress.saved_at) else ""
        if label.casefold() == "answer" and not answer:
            answer = words
    return legacy


def saved_native_answer(fields: dict[str, object], question: OpenQuestion, progress: QuestionProgress) -> str:
    if progress.state is QuestionDeliveryState.ANSWERED:
        return ""
    stored = cast(StoredDeliverySlots, cast(object, fields))
    answer, message = stored.get("user_answer"), stored.get("review_message")
    if answer is not None and answer.get("kind") in ("question", "followup"):
        source = Path(answer["source"])
        row = source.stem
        page = UserQuestionPage(source, Path(answer["production"]), (row,), "", answer)
    elif message is not None and not message.get("submission_request"):
        source = Path(message["source"])
        row = message.get("row", source.stem)
        page = ReviewMessagePage(source, source.parent, (row,), "", message)
    else:
        return ""
    return native_source_answer(page, row, question, progress)


def reconcile_native_answer(path: Path, fields: dict[str, object], question: OpenQuestion,
                            progress: QuestionProgress) -> bool:
    words = saved_native_answer(fields, question, progress)
    if not words:
        return False
    delivery_state.complete_review_reply(path, question.question_id, words)
    return True


def check_review_question_source(page: ReviewPage, row: str, question: OpenQuestion) -> None:
    """Check the record that owns the open Review exchange."""
    path = page.review_list
    try:
        _ = path.stat()
        if isinstance(page, (ReviewMessagePage, UserQuestionPage)) and (key := page.saved.get("question_source_key")):
            event = native_question_event(page, row)
            if event is not None and source_question_progress(path, row, event).attempt == key:
                return
            raise ClosedQuestionSource("The Review question is no longer open.")
        if isinstance(page, UserQuestionPage):
            current = saved_user_question(page)
            if current.status == 'open':
                followup = current.item.get('followup')
                if followup is not None and followup['text'] == question.text:
                    if 'review_submission' not in current.item:
                        return
                    submission = new_item_dialog.read_submission(current.item['review_submission'])
                    if submission.reply_token == question.question_id:
                        return
        elif isinstance(page, ReviewMessagePage):
            expected = (review_events.require_waiting_why(question.text)
                        if page.saved.get("question_save_uncertain") else None)
            if path.parent.name in ('investigations', 'archive') and re.fullmatch(r"I\d+", row):
                item = investigation.read_review_record(path)
                if item.id == row and item.state == investigation.InvestigationState.OPEN:
                    if isinstance(item.submission, new_item_dialog.ReviewSubmission):
                        if item.submission.reply_token == question.question_id:
                            return
                    elif expected is None or item.waiting_why == expected or any(
                            entry.words == "question: " + question.text for entry in item.log[-3:]):
                        return
            elif path.name.endswith('.review.json'):
                reviews = delivery_state.read_fields(path)
                if row in reviews:
                    from scripts.review_links.efficiency_reviews import decode
                    review = decode(reviews[row])
                    if review.mark == review_events.ReviewMark.WAITING and (
                            expected is None or review.waiting_why == expected):
                        return
            else:
                issue = read_recovery_proposal(path)
                if not proposal_issue.review_is_done(issue) and proposal_issue.review_mark(issue) == review_events.ReviewMark.WAITING:
                    submission = proposal_issue.review_submission(issue)
                    if isinstance(submission, new_item_dialog.ReviewSubmission):
                        if submission.reply_token == question.question_id:
                            return
                    elif expected is None or issue.value("waiting_why") == expected:
                        return
        else:
            snapshot = read_recovery_list(path)
            if any(record["#"] == row and not record.get("Verdict") for record in (*snapshot.pending, *snapshot.reviewed)):
                return
    except FileNotFoundError:
        pass
    raise ClosedQuestionSource("The Review question is no longer open.")


def review_question_source_lock(page: ReviewMessagePage | UserQuestionPage, row: str
                               ) -> AbstractContextManager[object]:
    """Use the same lock as the native Review answer writer."""
    path = page.review_list
    owner = staff.StaffId(page.saved["owner"])
    if isinstance(page, UserQuestionPage):
        return update_registration.outstanding_lock(update_registration.outstanding_path_for_owner(owner))
    if path.parent.name in ("investigations", "archive") and re.fullmatch(r"I\d+", row):
        return investigation.recipient_locks(owner)
    if path.name.endswith(".review.json"):
        return file_lock.exclusive(path.with_suffix(".lock"))
    return proposal_issue.issue_lock(path)


def question_progress_for_slots(page: ReviewPage, slots: DeliverySlots,
                                fields: dict[str, object]) -> QuestionProgress | None:
    """Keep the delivery result when a Review action changes the source."""
    progress = read_question_progress(fields["question_delivery"]) if "question_delivery" in fields else None
    question = slots.question
    if isinstance(question, AnsweredQuestion) and not isinstance(question, CompletedFeedback):
        if progress is not None and progress.question_id == question.question_id:
            return replace(progress, state=QuestionDeliveryState.ANSWERED)
        return None
    if not isinstance(question, OpenQuestion) or isinstance(question, OpenFeedback):
        return replace(progress, state=QuestionDeliveryState.CLOSED) if progress is not None else None
    intent = next((candidate for candidate in slots.intents() if review_open_question(page, slots, candidate)
                   == question), None)
    if progress is not None and progress.question_id == question.question_id:
        if intent is None or progress.attempt == intent.attempt:
            return progress
    if intent is None:
        return None
    return QuestionProgress(intent.attempt, question.question_id, time.time())


def review_progress(fields: dict[str, object], intent: DeliveryIntent, question: OpenQuestion) -> QuestionProgress:
    progress = read_question_progress(fields.get("question_delivery"))
    if progress.attempt != intent.attempt or progress.question_id != question.question_id:
        raise StaleQuestionAttempt("The Review question has changed.")
    return progress


def write_review_progress(path: Path, fields: dict[str, object], progress: QuestionProgress) -> None:
    content = json.dumps({**fields, "question_delivery": progress.stored()}) + "\n"
    atomic_file.write_text_atomic(path, content, new_file_mode=0o600)


def send_review_question(recipient: staff.StaffId, message: str, key: str, *,
                         sender: str = SENDER) -> QuestionSendReceipt:
    result = send_to_staff(recipient, sender, message, key=key, question=True)
    if result == "pending":
        raise UnconfirmedSend("The sender did not confirm question delivery.")
    return QuestionSendReceipt("kept" if result == "skipped" else "sent", "")


def send_saved_recipient(progress: QuestionProgress, delivery: MessageDelivery, recipient: staff.StaffId,
                         key: str, started: Callable[[], None]) -> QuestionProgress:
    result, receipt = attempt_question_send(
        progress, partial(send_review_question, recipient, delivery.message, key), started=started)
    if isinstance(receipt, QuestionSendReceipt):
        confirmed = (*progress.confirmed, recipient)
        state = (QuestionDeliveryState.CONFIRMED if all(target in confirmed for target in delivery.recipients)
                 else QuestionDeliveryState.NOT_STARTED)
        return replace(result, state=state, confirmed=confirmed)
    return result


def deliver_review_question(delivery: MessageDelivery, status: DeliveryStatus, intent: DeliveryIntent,
                            question: OpenQuestion) -> None:
    """Claim the saved Review question before sending it."""
    page, row = delivery.page, delivery.row
    path = status.path(page, row)
    try:
        with sender_claim(question_answer_lock_path(path, question.question_id)) as claimed:
            if not claimed:
                status.defer_question(path, delivery)
                return
            finish_review_question(delivery, status, path, intent, question)
    except StaleQuestionAttempt:
        return
    except (OSError, ValueError, KeyError, TypeError, AttributeError, change_review.ChangeReviewError,
            update_registration.RegistrationFailure, update_registration.InputError) as error:
        _defer_question_error(status, path, row, error, delivery=delivery)
    finally:
        status.release_recovery_claims((delivery.attempt,))


def finish_review_question(delivery: MessageDelivery, status: DeliveryStatus, path: Path,
                            intent: DeliveryIntent, question: OpenQuestion) -> None:
    page, row = delivery.page, delivery.row
    key = review_delivery_key(page.review_list, row, intent.attempt)
    while True:
        page = status.metadata_page(page)
        with ExitStack() as source_locks:
            if isinstance(page, (ReviewMessagePage, UserQuestionPage)):
                _ = source_locks.enter_context(review_question_source_lock(page, row))
            with status.locked():
                fields = read_recovery_delivery(status, path)
                slots = decode_change_delivery(fields)
                if intent not in slots.intents() or review_open_question(page, slots, intent) != question:
                    return
                progress = review_progress(fields, intent, question)
                if reconcile_native_answer(path, fields, question, progress):
                    status.forget_deferred_question(path)
                    return
                if not progress.state.pending:
                    break
                try:
                    check_review_question_source(page, row, question)
                except ClosedQuestionSource:
                    closed = replace(progress, state=QuestionDeliveryState.CLOSED)
                    write_review_progress(path, fields, closed)
                    status.finish_intent_locked(replace(delivery, page=page), slots, intent)
                    return
                setting_error = None
                original = progress
                if progress.deadline is None or progress.state is QuestionDeliveryState.NOT_STARTED:
                    progress, setting_error = progress.start_budget()
                if progress != original:
                    write_review_progress(path, fields, progress)
                if progress.state is QuestionDeliveryState.UNKNOWN:
                    status.defer_question(path, delivery)
                    return
                if (isinstance(page, (ReviewMessagePage, UserQuestionPage))
                        and (page.saved["message"] != delivery.message
                             or not (page.saved["saved"] or status.has_review_save_receipt(page, row, intent)))):
                    raise ValueError("The Review question save is unconfirmed.")
                recipient = next((target for target in delivery.recipients if target not in progress.confirmed), None)
                if setting_error is not None or recipient is None:
                    progress = progress.fail(setting_error or "No showrunner to send to.")
                    write_review_progress(path, fields, progress)
                    break

            def started() -> None:
                source_locks.close()
                with status.locked():
                    try:
                        current = read_recovery_delivery(status, path)
                        marked = review_progress(current, intent, question)
                    except (FileNotFoundError, StaleQuestionAttempt):
                        return
                    if marked == progress:
                        write_review_progress(path, current, begin_question_send(progress))

            result = send_saved_recipient(progress, delivery, recipient, key, started)
        with status.locked():
            fields = read_recovery_delivery(status, path)
            current = review_progress(fields, intent, question)
            if current not in (progress, begin_question_send(progress)):
                return
            write_review_progress(path, fields, result)
        if not result.state.pending:
            progress = result
            break
        if result.state is QuestionDeliveryState.UNKNOWN or result.error:
            status.defer_question(path, delivery)
            return

    current_delivery = replace(delivery, page=page)
    alerts: tuple[PendingDeliveryAlert, ...] = ()
    if progress.state is QuestionDeliveryState.FAILED:
        alert_recipients = delivery.alert_recipients
        if isinstance(page, (ReviewMessagePage, UserQuestionPage)):
            alert_recipients = review_alert_showrunners(staff.StaffId(page.saved["owner"]))
        recipient = next((target for target in delivery.recipients if target not in progress.confirmed), MissingRecipient())
        failure = DeliveryFailure(recipient, progress.error)
        alerts = pending_delivery_alerts(delivery, [failure], 1, alert_recipients, deadline=progress.deadline)
    with status.locked():
        slots = decode_change_delivery(read_recovery_delivery(status, path))
        if intent not in slots.intents() or review_open_question(page, slots, intent) != question:
            return
        if alerts:
            status.store_pending_alerts(current_delivery, alerts)
        status.finish_intent_locked(current_delivery, slots, intent)
        status.forget_deferred_question(path)
    if alerts:
        status.send_pending_alerts(status.closeout_path(page, row))


def deliver(delivery: MessageDelivery | NoMessageDelivery, status: DeliveryStatus, *, recovering_question: bool = False
            ) -> None:
    """Deliver each saved message and report exhausted attempts to its showrunner."""
    if isinstance(delivery, NoMessageDelivery):
        return
    if isinstance(delivery, PageQuestionSend):
        deliver_page_question(delivery, status)
        return
    with status.locked():
        path = status.path(delivery.page, delivery.row)
        ownership = status.request_attempts.get(delivery.attempt)
        if ownership is not None and ownership.state == "sending":
            if recovering_question:
                status.defer_question(path, delivery)
            return
        try:
            fields = read_recovery_delivery(status, path)
            slots = decode_change_delivery(fields)
            legacy = cast(StoredDeliverySlots, cast(object, fields)).get("user_answer")
            if legacy is not None and legacy.get("kind") == "followup":
                status.defer_question(path, UnreadableDeliveryRecord())
                return
            saved_progress = (read_question_progress(fields["question_delivery"])
                              if "question_delivery" in fields else None)
        except FileNotFoundError:
            _ = status.request_attempts.pop(delivery.attempt, None)
            return
        except (OSError, ValueError, KeyError, TypeError) as error:
            _ = status.request_attempts.pop(delivery.attempt, None)
            _defer_question_error(status, path, delivery.row, error, delivery=delivery)
            return
        intent = next((intent for intent in slots.intents() if intent.attempt == delivery.attempt), None)
        if intent is None:
            _ = status.request_attempts.pop(delivery.attempt, None)
            return
        page = delivery.page
        question = review_open_question(page, slots, intent)
        if isinstance(question, OpenQuestion) and saved_progress is None:
            _ = status.request_attempts.pop(delivery.attempt, None)
            status.defer_question(path, UnreadableDeliveryRecord())
            return
        if isinstance(question, NoQuestion):
            if saved_progress is not None and saved_progress.attempt == intent.attempt and saved_progress.state.pending:
                _ = status.request_attempts.pop(delivery.attempt, None)
                _defer_question_error(status, path, delivery.row, ValueError("Cannot read the saved Review question."),
                                      delivery=delivery)
                return
            if (recovering_question or native_review_question(page, slots, intent)
                    or (saved_progress is not None and saved_progress.attempt == intent.attempt)):
                current = replace(delivery, page=status.metadata_page(page))
                status.finish_intent_locked(current, slots, intent)
                status.forget_deferred_question(path)
                return
        status.request_attempts[delivery.attempt] = AttemptOwnership(status.path(page, delivery.row), "sending")
    if isinstance(question, OpenQuestion):
        deliver_review_question(delivery, status, intent, question)
        return
    deliver_change_message(delivery, status, path, intent)


def change_progress(fields: dict[str, object], intent: DeliveryIntent) -> QuestionProgress | None:
    attempts = fields.get("change_delivery", {})
    if not isinstance(attempts, dict):
        raise ValueError("Invalid Change delivery record.")
    raw = cast(dict[str, object], attempts).get(intent.kind)
    if raw is None:
        return None
    progress = read_question_progress(raw)
    return progress if progress.attempt == intent.attempt else None


@dataclass(frozen=True)
class ChangeRouteResult:
    state: QuestionDeliveryState = QuestionDeliveryState.NOT_STARTED
    error: str = ""


def change_route_results(fields: dict[str, object], intent: DeliveryIntent) -> dict[str, ChangeRouteResult]:
    slot = old_record(old_record(fields.get("change_delivery")).get(intent.kind))
    if slot.get("attempt") != intent.attempt:
        return {}
    return read_change_routes(slot)


def read_change_routes(slot: dict[str, object]) -> dict[str, ChangeRouteResult]:
    routes: dict[str, ChangeRouteResult] = {}
    for recipient, value in record_field(slot, "routes").items():
        route = old_record(value)
        state, error = route.get("state"), route.get("error")
        if not recipient.strip() or not isinstance(state, str) or not isinstance(error, str):
            raise ValueError("Invalid Change delivery route.")
        decoded = QuestionDeliveryState(state)
        if decoded in (QuestionDeliveryState.ANSWERED, QuestionDeliveryState.CLOSED):
            raise ValueError("Invalid Change delivery route state.")
        routes[recipient] = ChangeRouteResult(decoded, error)
    return routes


def write_change_progress(path: Path, fields: dict[str, object], intent: DeliveryIntent,
                          progress: QuestionProgress, routes: dict[str, ChangeRouteResult],
                          delivery: MessageDelivery) -> None:
    slot: dict[str, object] = {
        **progress.stored(), "message": delivery.message,
        "routes": {recipient: asdict(route) for recipient, route in routes.items()},
    }
    if intent.kind == "question":
        slot["feedback_source"] = {
            "path": str(delivery.page.review_list.resolve()), "row": delivery.row,
            "recipients": list(delivery.alert_recipients),
        }
    attempts = {**record_field(fields, "change_delivery"), intent.kind: slot}
    content = json.dumps({**fields, "change_delivery": attempts}) + "\n"
    atomic_file.write_text_atomic(path, content, new_file_mode=0o600)


def change_route_summary(progress: QuestionProgress, routes: dict[str, ChangeRouteResult]) -> QuestionProgress:
    confirmed = tuple(recipient for recipient, route in routes.items()
                      if route.state is QuestionDeliveryState.CONFIRMED)
    progress = replace(progress, confirmed=confirmed)
    for state in (QuestionDeliveryState.UNKNOWN, QuestionDeliveryState.NOT_STARTED, QuestionDeliveryState.FAILED):
        route = next((route for route in routes.values() if route.state is state), None)
        if route is not None:
            return replace(progress, state=state, error=route.error)
    return replace(progress, state=QuestionDeliveryState.CONFIRMED, error="")


def deliver_change_message(delivery: MessageDelivery, status: DeliveryStatus, path: Path,
                           intent: DeliveryIntent) -> None:
    """Send a saved Review message through the shared classifier."""
    retry = delivery if intent.kind == "question" else UnreadableDeliveryRecord()
    try:
        with sender_claim(question_answer_lock_path(path, intent.attempt)) as claimed:
            if not claimed:
                status.defer_question(path, retry)
                return
            finish_change_message(delivery, status, path, intent)
    except (OSError, ValueError) as error:
        _defer_question_error(status, path, delivery.row, error, delivery=retry)
    finally:
        status.release_recovery_claims((intent.attempt,))


def old_message_progress(status: DeliveryStatus, delivery: MessageDelivery, intent: DeliveryIntent) -> QuestionProgress:
    fields = status.closeout_fields(delivery.page, delivery.row)
    closeout = cast(dict[str, object], cast(object, fields))
    dispatch = old_record(closeout.get("review_dispatch"))
    state, error = old_result(intent.attempt, closeout, dispatch)
    return QuestionProgress(intent.attempt, intent.attempt, time.time(), state=state, error=error)


def finish_change_message(delivery: MessageDelivery, status: DeliveryStatus, path: Path,
                          intent: DeliveryIntent) -> None:
    key = review_delivery_key(delivery.page.review_list, delivery.row, delivery.attempt)
    tried: set[str] = set()
    while True:
        with status.locked():
            fields = read_recovery_delivery(status, path)
            if intent not in decode_change_delivery(fields).intents():
                return
            _ = status.closeout_fields(delivery.page, delivery.row)
            previous = change_progress(fields, intent)
            progress = previous or old_message_progress(status, delivery, intent)
            saved = old_record(old_record(fields.get("change_delivery")).get(intent.kind)) if previous is not None else {}
            original_message = saved.get("message")
            if isinstance(original_message, str):
                delivery = replace(delivery, message=original_message)
            routes = change_route_results(fields, intent)
            if progress.state is QuestionDeliveryState.NOT_STARTED and not routes:
                progress, error = progress.start_budget()
                recipients = progress.recipients if progress.recipients is not None else delivery.recipients
                progress = replace(progress, recipients=recipients)
                if recipients:
                    for recipient in recipients:
                        routes[recipient] = ChangeRouteResult() if error is None else ChangeRouteResult(
                            QuestionDeliveryState.FAILED, error)
                else:
                    progress = progress.fail(error or "No showrunner to send to.")
            deadline = progress.deadline
            if routes:
                if deadline is not None and time.time() >= deadline:
                    routes = {recipient: ChangeRouteResult(QuestionDeliveryState.FAILED, QUESTION_RECOVERY_EXPIRED)
                              if route.state.pending else route for recipient, route in routes.items()}
                progress = change_route_summary(progress, routes)
            else:
                # A malformed saved route has no recipient that can be sent to.
                progress = progress.expire()
            recipient = next((target for target, route in routes.items()
                              if route.state is QuestionDeliveryState.NOT_STARTED and target not in tried), None)
            if previous != progress or routes != change_route_results(fields, intent) or original_message is None:
                write_change_progress(path, fields, intent, progress, routes, delivery)
            if recipient is None:
                if progress.state.pending:
                    retry = delivery if intent.kind == "question" else UnreadableDeliveryRecord()
                    status.defer_question(path, retry)
                    return
                break
        marked_routes = {**routes, recipient: ChangeRouteResult(QuestionDeliveryState.UNKNOWN)}
        marker = change_route_summary(progress, marked_routes)

        def started() -> None:
            with status.locked():
                current = read_recovery_delivery(status, path)
                if (intent in decode_change_delivery(current).intents()
                        and change_progress(current, intent) == progress
                        and change_route_results(current, intent) == routes):
                    write_change_progress(path, current, intent, marker, marked_routes, delivery)

        tried.add(recipient)
        target = staff.StaffId(recipient)
        result, _ = attempt_question_send(replace(progress, confirmed=()),
                                          partial(send_review_question, target, delivery.message, key), started=started)
        with status.locked():
            fields = read_recovery_delivery(status, path)
            if (intent not in decode_change_delivery(fields).intents()
                    or (change_progress(fields, intent), change_route_results(fields, intent))
                    not in ((progress, routes), (marker, marked_routes))):
                return
            routes[recipient] = ChangeRouteResult(result.state, result.error)
            write_change_progress(path, fields, intent, change_route_summary(progress, routes), routes, delivery)

    failed: FailedDelivery | NoFailedDelivery = NO_FAILED_DELIVERY
    alerts: tuple[PendingDeliveryAlert, ...] = ()
    if progress.state is QuestionDeliveryState.FAILED:
        failed = FailedDelivery(delivery.attempt, delivery.page.review_list, delivery.row, progress.error)
        failures = [DeliveryFailure(staff.StaffId(target), route.error)
                    for target, route in routes.items() if route.state is QuestionDeliveryState.FAILED]
        if not failures:
            failures.append(DeliveryFailure(MissingRecipient(), progress.error))
        recipients = delivery.alert_recipients
        if isinstance(delivery.page, (ReviewMessagePage, UserQuestionPage)):
            recipients = review_alert_showrunners(staff.StaffId(delivery.page.saved["owner"]))
        alerts = pending_delivery_alerts(delivery, failures, 1, recipients)
        if not alerts:
            print(f"review-links delivery {delivery.row}: {progress.error}; no separate showrunner for the alert",
                  file=sys.stderr)
    with status.locked():
        fields = read_recovery_delivery(status, path)
        if intent not in decode_change_delivery(fields).intents() or change_progress(fields, intent) != progress:
            return
        if alerts:
            status.store_pending_alerts(delivery, alerts)
        status.record(delivery, failed)
        if status.slots(delivery.page, delivery.row).intents():
            status.defer_question(path, UnreadableDeliveryRecord())
        else:
            status.forget_deferred_question(path)
    if alerts:
        status.send_pending_alerts(status.closeout_path(delivery.page, delivery.row))


def pending_delivery_alerts(delivery: MessageDelivery, failures: list[DeliveryFailure], tries: int,
                            recipients: tuple[staff.StaffId, ...], *, deadline: float | None = None,
                            title: str = "Change review delivery needs attention"
                            ) -> tuple[PendingDeliveryAlert, ...]:
    source, row = delivery.page.review_list, delivery.row
    alerts = tuple(
        PendingDeliveryAlert(recipient=recipient,
            key=review_delivery_key(source, row, delivery.attempt, purpose=f"alert:{failure.recipient}"),
            message=delivery_failure_alert(failure, tries, source, row, delivery.message, title))
        for failure in failures for recipient in recipients if recipient != failure.recipient)
    if not alerts:
        return ()
    if deadline is None:
        deadline = question_delivery_deadline()
    return tuple({**alert, "deadline": deadline} for alert in alerts)


def delivery_failure_alert(failure: DeliveryFailure, tries: int, source: Path, row: str, message: str,
                           title: str = "Change review delivery needs attention") -> str:
    target = "no recipient" if isinstance(failure.recipient, MissingRecipient) else failure.recipient
    return "\n".join((title, f"List: {source}", f"Row: {row}", f"Recipient: {target}", f"Tries: {tries}",
                      f"Last error: {failure.error}", "The user's answer is saved. Pass it on by hand.", message))


def alert_delivery_failures(failures: list[DeliveryFailure], tries: int, *, status: DeliveryStatus,
                            source: Path, row: str, message: str, recipients: tuple[staff.StaffId, ...],
                            attempt: str, title: str = "Change review delivery needs attention") -> bool:
    page = ReviewPage(source, source.parent, (row,), "")
    delivery = MessageDelivery(page, row, (), message, attempt)
    alerts = pending_delivery_alerts(delivery, failures, tries, recipients, title=title)
    if not alerts:
        for failure in failures:
            print(f"review-links delivery {row}: {failure.error}; no separate showrunner for the alert", file=sys.stderr)
        return False
    path = status.closeout_path(page, row)
    with status.locked():
        status.store_pending_alerts(delivery, alerts)
    status.send_pending_alerts(path)
    keys = {(alert.get("key"), alert["recipient"]) for alert in alerts}
    with status.locked():
        return any(alert.get("state") == QuestionDeliveryState.CONFIRMED
                   and (alert.get("key"), alert["recipient"]) in keys
                   for alert in status.closeout_fields(page, row).get("pending_alerts", []))



def recover_verdict_cleanups(status: DeliveryStatus, intents: tuple[Path, ...],
                             unreadable: set[Path]) -> set[Path]:
    """Finish each durable verdict and return the deliveries that must wait."""
    unready: set[Path] = set()
    for intent in intents:
        path = intent.with_name(DELIVERY_PREFIX + intent.name.removeprefix(change_review.VERDICT_CLEANUP_PREFIX))
        try:
            if intent.is_symlink():
                continue
            with status.locked():
                cleanup = change_review.PendingVerdictCleanup.load(intent)
                listing = cleanup.path
                if listing in unreadable:
                    unready.add(path)
                    status.defer_cleanup(intent, OSError(f"Cannot read {listing}."))
                    continue
                path = change_routing.delivery_path(status.root, listing, cleanup.row)
                if status.migration_pending(path):
                    unready.add(path)
                    status.defer_cleanup(intent, ValueError("The delivery record needs migration."))
                    continue
                with change_review.checked_review_write(listing, status.root, lambda: None):
                    if any(ownership.row_file == path for ownership in status.request_attempts.values()):
                        continue
                    try:
                        snapshot = read_recovery_list(listing)
                    except (OSError, ValueError, change_review.ChangeReviewError) as error:
                        unreadable.add(listing)
                        unready.add(path)
                        status.defer_cleanup(intent, error)
                        continue
                    saved = any(row['#'] == cleanup.row and row['Verdict'] == cleanup.verdict for row in snapshot.reviewed)
                    if not saved:
                        intent.unlink(missing_ok=True)
                        continue
                    try:
                        cleanup.confirm_durability()
                    except change_review.VerdictDurabilityUnconfirmed as error:
                        unready.add(path)
                        status.defer_cleanup(intent, error)
                        continue
            for line in finish_saved_verdict(cleanup, status):
                print(f"review-links {line}", file=sys.stderr)
        except Exception as error:
            if isinstance(error, FileNotFoundError) and cast(object, error.filename) == str(intent):
                continue
            unready.add(path)
            status.defer_cleanup(intent, error)
    for path in unready:
        status.defer_question(path, UnreadableDeliveryRecord())
    return unready


def recover_change_question_sources(status: DeliveryStatus) -> None:
    """Restore delivery records for open questions in the Change lists."""
    folder = change_review.lists_directory()
    snapshots: dict[Path, change_review.ReviewListSnapshot] = {}
    for listing in source_paths(folder, change_review.PREFIX + "*.md", sources=[]):
        try:
            snapshots[listing] = read_recovery_list(listing, include_reviewed=False)
        except (OSError, ValueError, change_review.ChangeReviewError) as error:
            status.question_sources_pending.set()
            status.report_recovery_error(listing, error, f"question source {listing}")
    routing: change_routing.StaffRouting | None = None
    for listing, snapshot in snapshots.items():
        for row in (*snapshot.pending, *snapshot.reviewed):
            if row.get("Verdict"):
                continue
            identity = row["#"]
            try:
                event = change_question_event(row)
                if event is None:
                    continue
                with status.locked():
                    text = question_words(event)
                    path = change_routing.delivery_path(status.root, listing, identity)
                    if status.migration_pending(path):
                        continue
                    try:
                        fields = read_recovery_delivery(status, path)
                    except FileNotFoundError:
                        fields = {}
                    progress = source_question_progress(listing, identity, event)
                    if not needs_source_question(fields, progress, text):
                        continue
                    current = next((entry for entry in read_recovery_list(listing).pending
                                    if entry["#"] == identity), None)
                    if current is None or current.get("Verdict") or change_question_event(current) != event:
                        continue
                    slots = decode_change_delivery(fields)
                    if isinstance(slots.verdict, DeliveryIntent):
                        status.question_sources_pending.set()
                        continue
                    if routing is None:
                        routing = list_routing(snapshots)
                    assignment = routing.owner_and_production(listing, row)
                    if isinstance(assignment.production, change_routing.UnavailableProduction):
                        raise ValueError("the production document is not available")
                    page = AssignedReviewPage(listing, assignment.production, (identity,), "", assignment.owner)
                    recipients = (status.validators(page, identity) if slots.validation_requested
                                  else assignment.owner.showrunner_recipients)
                    _ = status.ask_locked(page, identity, slots, text, recipients, "question", event=event,
                                          during_validation=slots.validation_requested, claim=False)
                    status.defer_question(path, UnreadableDeliveryRecord())
            except (OSError, ValueError, KeyError, change_review.ChangeReviewError) as error:
                status.question_sources_pending.set()
                status.report_recovery_error(listing, error, f"question source {listing}/{identity}")


def recover_question_sources(status: DeliveryStatus) -> None:
    """Find questions whose source saved before its delivery record."""
    list_scan, images_scan, review_scan = (
        cast(Callable[[DeliveryStatus], None], import_module(f"scripts.review_links.{module}").recover_questions)
        for module in ("list_page", "images", "review_actions"))
    status.question_sources_pending.clear()
    for scan in (list_scan, images_scan, recover_change_question_sources, review_scan):
        try:
            scan(status)
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError, change_review.ChangeReviewError,
                update_registration.InputError, update_registration.RegistrationFailure) as error:
            status.question_sources_pending.set()
            status.report_recovery_error(status.root / scan.__name__, error, "question sources")


def question_delivery_deadline() -> float:
    progress, _ = QuestionProgress("", "", time.time()).start_budget()
    assert progress.deadline is not None
    return progress.deadline


def old_recovery(path: Path) -> dict[str, object]:
    recovery = path.with_name(QUESTION_RECOVERY_PREFIX + path.name.removeprefix(DELIVERY_PREFIX))
    try:
        fields = read_delivery_object(recovery)
    except FileNotFoundError:
        return {}
    check_text_fields(fields, ("attempt", "question_id", "error"))
    if "deadline" in fields:
        check_delivery_number(fields, "deadline")
    return fields


def migrated_progress(recovery: dict[str, object], attempt: str, identity: str, saved_at: float,
                      deadline: float, state: QuestionDeliveryState, error: str = "",
                      confirmed: tuple[str, ...] = (), *, finished: bool = False) -> QuestionProgress:
    """Keep the matching deadline and final result of an old attempt."""
    if recovery.get("attempt") == attempt or (
            recovery.get("attempt") == "" and recovery.get("question_id") in ("", identity)):
        limit = recovery.get("deadline")
        if isinstance(limit, (int, float)) and not isinstance(limit, bool) and math.isfinite(limit):
            deadline = limit
        reason = recovery.get("error")
        if state.pending and isinstance(reason, str) and reason:
            state, error = QuestionDeliveryState.FAILED, reason
    if state is QuestionDeliveryState.NOT_STARTED and finished:
        state = QuestionDeliveryState.CONFIRMED
    return QuestionProgress(attempt, identity, saved_at, deadline, state, error, confirmed)


def migrate_page_attempt(status: DeliveryStatus, path: Path, record: QuestionAttempt,
                         saved_at: float, deadline: float) -> None:
    """Keep the receipt or make the old pending attempt ready."""
    state = page_state(record)
    if state.pending:
        state = QuestionDeliveryState.NOT_STARTED
    progress = migrated_progress(old_recovery(path), record.attempt_id, record.source.question_id, saved_at,
                                 deadline, state, record.error if not state.pending else "")
    if page_source_answered(status, record):
        progress = replace(progress, state=QuestionDeliveryState.ANSWERED, error="")
    publish_migrated_page(path, record, replace(progress, recipients=(record.recipient_id,)))


def publish_migrated_page(path: Path, record: QuestionAttempt, progress: QuestionProgress, key: str = "") -> None:
    receipt = record.receipt
    if progress.state is QuestionDeliveryState.CONFIRMED and isinstance(receipt, NoQuestionSendReceipt):
        receipt = QuestionSendReceipt("sent", "")
    publish_question_attempt(path, PageAttempt(
        record.source, record.recipient_id, record.message, record.revision + 1,
        record.state, record.attempt_id,
        receipt=receipt if progress.state is QuestionDeliveryState.CONFIRMED else NO_QUESTION_SEND_RECEIPT,
        progress=progress, legacy_key=key))


def old_record(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def record_field(fields: dict[str, object], name: str) -> dict[str, object]:
    """Distinguish a missing object from an unreadable object."""
    return require_record(fields.get(name, {}), name)


def require_record(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"Invalid delivery field {name}.")
    return cast(dict[str, object], value)


def check_text_fields(fields: dict[str, object], optional: tuple[str, ...],
                      *, required: tuple[str, ...] = ()) -> None:
    for name in (*required, *(key for key in optional if key in fields)):
        if not isinstance(fields.get(name), str):
            raise ValueError(f"Invalid delivery field {name}.")


def check_delivery_number(fields: dict[str, object], name: str) -> None:
    if not delivery_number(fields.get(name)):
        raise ValueError(f"Invalid delivery field {name}.")


def delivery_number(value: object) -> bool:
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def check_intent_fields(fields: dict[str, object], name: str, *, legacy: bool = False) -> None:
    check_text_fields(fields, ("feedback", "error", "last_error"))
    state = fields.get("state", "sent" if legacy else None)
    states = ("pending",) if name == "question" else ("pending", "sent", "confirmed", "failed", "answered")
    if state not in states:
        raise ValueError(f"Invalid {name} delivery state.")
    attempt = fields.get("attempt", "" if legacy else None)
    if not isinstance(attempt, str) or (state == "pending" and not attempt.strip()):
        raise ValueError(f"Invalid {name} delivery attempt.")
    _ = migrated_recipients(fields)


def check_dispatch_fields(fields: dict[str, object], name: str) -> None:
    if name not in fields:
        return
    dispatch = record_field(fields, name)
    check_text_fields(dispatch, ("question_id", "error", "sending_to"), required=("attempt",))
    if dispatch.get("state") not in ("started", "started_after_unconfirmed", "accepted", "finished", "failed", "unknown"):
        raise ValueError(f"Invalid delivery field {name}.")
    if any(value not in ("sent", "unknown") for value in record_field(dispatch, "recipients").values()):
        raise ValueError(f"Invalid delivery recipients in {name}.")
    if "keyed" in dispatch and not isinstance(dispatch["keyed"], bool):
        raise ValueError(f"Invalid delivery field {name}.")


def read_closeout_fields(path: Path) -> StoredDeliveryCloseout:
    fields = read_delivery_object(path)
    check_text_fields(fields, (), required=("review_list", "row"))
    messages = fields.get("messages")
    if not isinstance(messages, list) or not all(isinstance(word, str) for word in cast(list[object], messages)):
        raise ValueError("Invalid delivery closeout messages.")
    if "failed_delivery" in fields:
        check_text_fields(record_field(fields, "failed_delivery"), (),
                          required=("attempt", "review_list", "row", "last_error"))
    for name in ("review_dispatch", "question_dispatch"):
        check_dispatch_fields(fields, name)
    alerts = fields.get("pending_alerts", [])
    if not isinstance(alerts, list):
        raise ValueError("Invalid pending delivery alerts.")
    for value in cast(list[object], alerts):
        alert = require_record(value, "alert")
        check_text_fields(alert, ("key",), required=("recipient", "message"))
        if "deadline" in alert:
            check_delivery_number(alert, "deadline")
        if "state" in alert and alert["state"] not in (QuestionDeliveryState.NOT_STARTED, QuestionDeliveryState.UNKNOWN,
                                                       QuestionDeliveryState.CONFIRMED, QuestionDeliveryState.FAILED):
            raise ValueError("Invalid pending alert state.")
    return cast(StoredDeliveryCloseout, cast(object, fields))


def checked_delivery_fields(path: Path) -> dict[str, object]:
    """Validate the saved evidence before a recovery decision."""
    fields = read_delivery_object(path)
    if "question_attempt" in fields:
        data = record_field(fields, "question_attempt")
        _ = decode_page_attempt(fields) if data.get("schema") == 3 else delivery_state.decode_question_attempt(fields)
        return fields
    check_text_fields(fields, ("handed_at", "waiting_why", "kind"))
    for name in ("verdict", "validation"):
        if name in fields:
            check_intent_fields(record_field(fields, name), name)
    if fields.get("kind") in ("verdict", "validation") or "feedback" in fields:
        check_intent_fields(fields, "legacy", legacy=True)
    if "question" in fields:
        exchange = record_field(fields, "question")
        check_text_fields(exchange, ("answer", "kind"), required=("question_id", "text"))
        if not exchange["question_id"] or not cast(str, exchange["text"]).strip():
            raise ValueError("Invalid saved Review question.")
        if exchange.get("kind", "feedback") != "feedback":
            raise ValueError("Invalid saved Review question kind.")
        if exchange.get("delivery") is not None:
            check_intent_fields(question_intent_fields(fields), "question")
    for name in ("review_message", "user_answer"):
        if name not in fields:
            continue
        source = record_field(fields, name)
        required = ("source", "owner", "message")
        if name == "user_answer":
            required = (*required, "production")
        check_text_fields(source, ("row", "submission_request", "question_source_key", "question_id",
                                   "question_text", "question", "answer", "kind"), required=required)
        for key in ("saved", "submission_delivered", "question_save_uncertain", "legacy_followup"):
            if (key == "saved" or key in source) and not isinstance(source.get(key), bool):
                raise ValueError(f"Invalid delivery field {name}.{key}.")
        if name == "user_answer" and source.get("kind", "answer") not in ("answer", "followup", "skip", "question", "feedback"):
            raise ValueError("Invalid saved user answer kind.")
        check_dispatch_fields(source, "image_question_dispatch")
    slots = decode_change_delivery(fields)
    intents: dict[str, str] = {intent.kind: intent.attempt for intent in slots.intents()}
    question = slots.question
    if "question_delivery" in fields:
        progress = read_question_progress(fields["question_delivery"])
        if isinstance(question, OpenQuestion) and not isinstance(question, OpenFeedback):
            native = ("review_message" in fields and not record_field(fields, "review_message").get("submission_request")
                      or record_field(fields, "user_answer").get("kind") in ("question", "followup"))
            attempt = intents.get("question") or (intents.get("verdict") if native else None)
            if attempt is not None and (progress.attempt, progress.question_id) != (attempt, question.question_id):
                raise ValueError("The saved Review question progress does not match its intent.")
    for kind, value in record_field(fields, "change_delivery").items():
        progress = read_question_progress(value)
        if kind in intents and progress.attempt != intents[kind]:
            raise ValueError("The saved Change delivery progress does not match its intent.")
        slot = require_record(value, kind)
        check_text_fields(slot, ("message",))
        _ = read_change_routes(slot)
        if "feedback_source" in slot:
            source = record_field(slot, "feedback_source")
            check_text_fields(source, (), required=("path", "row"))
            check_text_fields(slot, (), required=("message",))
            if (kind != "question" or migrated_recipients(source) is None
                    or migrated_recipients(slot) is None):
                raise ValueError("Invalid saved Change feedback source.")
    return fields


def read_delivery_object(path: Path) -> dict[str, object]:
    try:
        return delivery_state.read_fields(path)
    except FileNotFoundError as error:
        if cast(object, error.filename) == str(path):
            raise
        raise OSError(f"Cannot read {path}: {error}") from error
    except RecursionError as error:
        raise ValueError(f"Cannot decode {path}: {error}") from error


def old_result(attempt: str, closeout: dict[str, object], dispatch: dict[str, object]
               ) -> tuple[QuestionDeliveryState, str]:
    failure = old_record(closeout.get("failed_delivery"))
    if failure.get("attempt") == attempt and isinstance(reason := failure.get("last_error"), str):
        return QuestionDeliveryState.FAILED, reason
    if dispatch.get("attempt") == attempt:
        if dispatch.get("state") in ("finished", "accepted"):
            return QuestionDeliveryState.CONFIRMED, ""
        if dispatch.get("state") == "failed":
            reason = dispatch.get("error")
            return QuestionDeliveryState.FAILED, reason if isinstance(reason, str) else ""
    return QuestionDeliveryState.NOT_STARTED, ""


def migrate_old_page_message(status: DeliveryStatus, path: Path, saved: StoredReviewMessage,
                             fields: dict[str, object], closeout: dict[str, object], deadline: float) -> None:
    row = saved.get("row", "")
    question_id = row.rsplit("/", 1)[-1]
    source = Path(saved["source"])
    recipient = staff.StaffId(saved["owner"])
    if row.startswith(IMAGE_QUESTION_ROW_PREFIX):
        folder = status.root / image_store.IMAGES_FOLDER / source.parent.name
        if source.resolve() != (folder / image_store.RECORD).resolve():
            raise ValueError("The Images question source has moved.")
        image = image_store.read_record(folder)
        target = image_question_target(source, image)
        questions = tuple(question_record.read(value) for value in image.get("questions", []))
    else:
        listing = record_store.NumberedRecords(recipient, "list", "L", "list item", ValueError)
        if source.resolve() != listing.path(source.stem).resolve():
            raise ValueError("The List question source has moved.")
        item = listing.read(source.stem, path=source).fields
        target = list_question_target(source, item["id"], int(item["number"]), item["title"], recipient)
        questions = _read_list_question_source(item)
    question = next((entry for entry in questions if entry.question_id == question_id), None)
    if question is None:
        raise ValueError("The delivery record has no matching source question.")
    current = target.saved(question)
    if current.source.row != row or status.path(current.page(), row) != path:
        raise ValueError("The delivery record has a different source row.")
    raw = old_record(fields.get("verdict"))
    dispatch = old_record(saved.get("image_question_dispatch")) or old_record(closeout.get("review_dispatch"))
    failure = old_record(closeout.get("failed_delivery"))
    recovery = old_recovery(path)
    attempt = raw.get("attempt", dispatch.get("attempt", failure.get("attempt", recovery.get("attempt"))))
    key = review_delivery_key(source, row, attempt) if isinstance(attempt, str) and attempt else ""
    if not isinstance(attempt, str) or not attempt:
        attempt = source_question_key(current.source.path, row, question.asked_at, question.words)
    state, error = old_result(attempt, closeout, dispatch)
    if question.answer is not None:
        state, error = QuestionDeliveryState.ANSWERED, ""
    recipients = migrated_recipients(raw)
    if state.pending and recipients is not None and recipients != (recipient,):
        raise ValueError("The saved recipients do not match the question.")
    record = QuestionAttempt(current.source, current.recipient, current.message, 1, QuestionSendState.READY, attempt)
    saved_at = datetime.fromisoformat(question.asked_at).timestamp()
    progress = migrated_progress(recovery, attempt, question_id, saved_at, deadline, state, error,
                                 finished=not raw and not dispatch and saved["saved"])
    publish_migrated_page(path, record, replace(progress, recipients=recipients), key)


def malformed_recipients(intent: dict[str, object]) -> bool:
    if "recipients" not in intent:
        return False
    values = intent["recipients"]
    return not isinstance(values, list) or not all(
        isinstance(value, str) and bool(value.strip()) for value in cast(list[object], values))


def migrated_recipients(fields: dict[str, object]) -> tuple[str, ...] | None:
    if "recipients" not in fields:
        return None
    if malformed_recipients(fields):
        raise ValueError("The saved recipients are unreadable.")
    return tuple(cast(list[str], fields["recipients"]))


def migrate_review_attempt(path: Path, fields: dict[str, object], slots: DeliverySlots,
                           closeout: dict[str, object], saved_at: float, deadline: float) -> None:
    saved = cast(StoredDeliverySlots, cast(object, fields)).get("user_answer")
    question = slots.question
    followup = saved is not None and saved.get("kind") == "followup"
    native = "review_message" in fields or saved is not None and (followup or saved.get("kind") == "question")
    raw = old_record(fields.get("verdict")) if native else question_intent_fields(fields)
    dispatch = old_record(closeout.get("question_dispatch"))
    if followup and not dispatch:
        dispatch = old_record(closeout.get("review_dispatch"))
    fallback = question.question_id if isinstance(question, (OpenQuestion, AnsweredQuestion)) else path.name
    if isinstance(question, (OpenQuestion, AnsweredQuestion)) and dispatch.get("question_id") not in (
            None, question.question_id):
        dispatch = {}
    recovery = old_recovery(path)
    failure = old_record(closeout.get("failed_delivery"))
    prior = failure.get("attempt") or recovery.get("attempt") or fallback
    attempt = raw.get("attempt", dispatch.get("attempt", prior))
    if not isinstance(attempt, str) or not attempt:
        raise ValueError("The old delivery record has no attempt ID.")
    if followup:
        assert saved is not None
        source = Path(saved["source"])
        row = source.stem
        page = UserQuestionPage(source, Path(saved["production"]), (row,), "", saved)
        current = open_user_question(page)
        event = user_question_event(current) if current is not None else None
        identity = attempt[:8]
        words = saved.get("question_text", "") or saved["message"]
        updated: StoredUserAnswer = {**saved, "kind": "question", "legacy_followup": True}
        if event is not None and event.time.timestamp() <= saved_at and question_words(event).strip() in saved["message"]:
            words = question_words(event)
            proof = source_question_progress(source, row, event)
            identity = proof.question_id
            updated["question_source_key"] = proof.attempt
            saved_at = proof.saved_at
        elif current is not None:
            pending = current.item.get("followup")
            if pending is not None and pending["text"] in saved["message"]:
                words = pending["text"]
            replies = current.item.get("followups", [])
            if pending is None and replies and replies[-1]["followup"] in saved["message"]:
                question = AnsweredQuestion(identity, replies[-1]["followup"], replies[-1]["reply"])
        if not isinstance(question, AnsweredQuestion):
            question = OpenQuestion(identity, words, NoDelivery())
        updated.update(question_id=question.question_id, question_text=question.text)
        fields["user_answer"] = updated
        fields["question"] = ({"question_id": question.question_id, "text": question.text, "answer": question.answer}
                              if isinstance(question, AnsweredQuestion) else
                              {"question_id": question.question_id, "text": question.text, "delivery": None})
    if not isinstance(question, (OpenQuestion, AnsweredQuestion)):
        return
    state, error = old_result(attempt, closeout, dispatch)
    if isinstance(question, AnsweredQuestion):
        state, error = QuestionDeliveryState.ANSWERED, ""
    elif state is QuestionDeliveryState.NOT_STARTED and not native:
        name = path.name.removeprefix(DELIVERY_PREFIX)
        digest = hashlib.sha256(attempt.encode()).hexdigest()
        receipt = path.with_name(f".review-link-saved-{name}-{digest}")
        try:
            if receipt.read_text(encoding="utf-8").strip() == attempt:
                state = QuestionDeliveryState.CONFIRMED
        except FileNotFoundError:
            pass
    receipts = old_record(dispatch.get("recipients")) if dispatch.get("attempt") == attempt else {}
    confirmed = tuple(recipient for recipient, result in receipts.items() if result == "sent")
    progress = migrated_progress(recovery, attempt, question.question_id, saved_at, deadline, state, error,
                                 confirmed, finished=not raw and not dispatch)
    fields["question_delivery"] = replace(progress, recipients=migrated_recipients(raw)).stored()


def migrate_delivery_file(status: DeliveryStatus, path: Path, deadline: float | None = None) -> None:
    """Convert an older delivery record under the delivery lock."""
    if path.is_symlink():
        return
    with status.locked():
        fields = checked_delivery_fields(path)
        page_attempt = old_record(fields.get("question_attempt"))
        if page_attempt.get("schema") == 3:
            return
        saved_at = path.stat().st_mtime
        if "question_attempt" in fields:
            record = delivery_state.decode_question_attempt(fields)
            migrate_page_attempt(status, path, record, saved_at, deadline or question_delivery_deadline())
            return
        stored = cast(StoredDeliverySlots, cast(object, fields))
        saved_page = stored.get("review_message")
        slots = decode_change_delivery(fields)
        answer = stored.get("user_answer")
        old_page = saved_page is not None and saved_page.get("row", "").startswith((IMAGE_QUESTION_ROW_PREFIX, "List item "))
        old_question = (isinstance(slots.question, (OpenQuestion, AnsweredQuestion))
                        and not isinstance(slots.question, (OpenFeedback, CompletedFeedback))
                        or answer is not None and answer.get("kind") == "followup")
        native_question = old_question and (saved_page is not None or answer is not None)
        old_message = (not native_question
                       and (fields.get("kind") in ("verdict", "validation") or "feedback" in fields
                            or any(kind in fields and kind not in old_record(fields.get("change_delivery"))
                                   for kind in ("verdict", "validation"))))
        if not (old_page or old_question or old_message):
            return
        try:
            closeout = cast(dict[str, object], cast(object, read_closeout_fields(question_closeout_path(path))))
        except FileNotFoundError:
            closeout = {}
        limit = deadline or question_delivery_deadline()
        if old_page and saved_page is not None:
            migrate_old_page_message(status, path, saved_page, fields, closeout, limit)
            return
        previous = json.dumps(fields)
        if old_question and "question_delivery" not in fields:
            migrate_review_attempt(path, fields, slots, closeout, saved_at, limit)
        if native_question:
            question = decode_change_delivery(fields).question
            if "question_delivery" in fields and isinstance(question, OpenQuestion):
                progress = read_question_progress(fields["question_delivery"])
                if words := saved_native_answer(fields, question, progress):
                    _ = delivery_state.apply_review_reply(fields, question.question_id, words)
        if old_message:
            migrate_old_message(path, fields, closeout, saved_at, limit)
        content = json.dumps(fields)
        if content != previous:
            try:
                atomic_file.write_text_atomic(path, content + "\n", new_file_mode=0o600)
            except atomic_file.AtomicDurabilityUnconfirmed:
                if delivery_state.read_fields(path) != fields:
                    raise


def migrate_old_message(path: Path, fields: dict[str, object], closeout: dict[str, object],
                        saved_at: float, deadline: float) -> None:
    """Convert each old message slot without changing another slot."""
    kind = fields.get("kind")
    if kind is None and "feedback" in fields:
        kind = "validation"
    if isinstance(kind, str) and kind in ("verdict", "validation"):
        fields[kind] = {key: fields.pop(key) for key in ("attempt", "state", "feedback", "recipients", "error", "last_error")
                        if key in fields}
        _ = old_record(fields[kind]).setdefault("attempt", "")
        _ = old_record(fields[kind]).setdefault("state", "sent")
        _ = fields.pop("kind", None)
    recovery = old_recovery(path)
    attempts = dict(old_record(fields.get("change_delivery")))
    for kind in ("verdict", "validation"):
        slot = old_record(fields.get(kind))
        attempt, state = slot.get("attempt"), slot.get("state")
        if kind in attempts or not slot:
            continue
        if not isinstance(attempt, str) or (not attempt and state == "pending"):
            raise ValueError(f"The old {kind} has no attempt ID.")
        result, reason = old_result(attempt, closeout, old_record(closeout.get("review_dispatch")))
        if state == "failed":
            result = QuestionDeliveryState.FAILED
            error = slot.get("error", slot.get("last_error"))
            if isinstance(error, str) and error:
                reason = error
        elif state in ("confirmed", "sent") and result.pending:
            result = QuestionDeliveryState.CONFIRMED
        elif state == "answered":
            result = QuestionDeliveryState.ANSWERED
        progress = migrated_progress(recovery, attempt, attempt, saved_at, deadline, result, reason)
        recipients = migrated_recipients(slot)
        attempts[kind] = replace(progress, recipients=recipients).stored()
        native_verdict = kind == "verdict" and ("review_message" in fields or "user_answer" in fields)
        if progress.state.pending or native_verdict:
            slot["state"] = "pending"
        elif kind == "validation":
            slot["state"] = "sent"
        else:
            _ = fields.pop(kind)
    fields["change_delivery"] = attempts


def recover_delivery_migration(status: DeliveryStatus, path: Path, deadline: float | None = None) -> bool:
    """Keep an unreadable delivery record scheduled until migration succeeds."""
    try:
        migrate_delivery_file(status, path, deadline)
    except Exception as error:
        missing = isinstance(error, FileNotFoundError) and cast(object, error.filename) == str(path)
        if not missing:
            with status.deferred_delivery_lock:
                status.deferred_migrations.add(path)
            status.report_recovery_error(path, error, f"delivery migration {path.name}")
            return False
    with status.deferred_delivery_lock:
        status.deferred_migrations.discard(path)
    return True


def migrate_question_deliveries(status: DeliveryStatus) -> None:
    """Convert saved questions and alerts before startup recovery."""
    if status.delivery_migration_done:
        return
    deadline = question_delivery_deadline()
    for path in status.read_view().paths:
        _ = recover_delivery_migration(status, path, deadline)
    for path in status.root.glob(CLOSEOUT_PREFIX + "*.json"):
        if path.is_symlink():
            continue
        try:
            with status.locked():
                _ = status.migrate_alerts(path, deadline)
        except FileNotFoundError:
            continue
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
            with status.deferred_delivery_lock:
                status.deferred_alerts.add(path)
            status.report_recovery_error(path, error, f"delivery alert {path.name}")
    status.delivery_migration_done = True


def recover_pending(status: DeliveryStatus, *,
                    only: change_review.PendingVerdictCleanup | change_review.NoVerdictCleanup = change_review.NO_VERDICT_CLEANUP) -> None:
    """Recover scratch cleanup before the saved deliveries."""
    targeted = isinstance(only, change_review.PendingVerdictCleanup)
    if not targeted:
        migrate_question_deliveries(status)
        recover_question_sources(status)
        _ = expire_saved_questions(status, status.recovery_paths())
        for path in status.root.glob(CLOSEOUT_PREFIX + "*.json"):
            if not path.is_symlink():
                status.send_pending_alerts(path)
    intents = (only.intent,) if targeted else tuple(sorted(status.root.glob(change_review.VERDICT_CLEANUP_PREFIX+'*')))
    if not intents and not status.read_view().paths:
        return
    unreadable: set[Path] = set()
    unready = recover_verdict_cleanups(status, intents, unreadable)
    if not targeted:
        recover_question_answers(status)
        recover_review_messages(status)
    if targeted:
        paths = frozenset({change_routing.delivery_path(status.root, only.path, only.row)})
        listings: Iterable[Path] = (only.path,)
    else:
        paths = status.recovery_paths()
        listings = recovery_listings(status, paths)
    recover_change_messages(status, paths, listings, unreadable, unready, only)


def read_recovery_delivery(status: DeliveryStatus, path: Path) -> dict[str, object]:
    """Schedule another read when the saved delivery record is unavailable."""
    try:
        return checked_delivery_fields(path)
    except FileNotFoundError:
        raise
    except (OSError, ValueError, OverflowError) as error:
        _defer_question_error(status, path, path.name, error, delivery=UnreadableDeliveryRecord())
        raise


def defer_unmatched_change_questions(status: DeliveryStatus, paths: Iterable[Path]) -> None:
    """Keep pending Change deliveries when recovery cannot read all lists."""
    for path in paths:
        try:
            if path.is_symlink():
                continue
            fields = read_recovery_delivery(status, path)
            if {"question_attempt", "review_message", "user_answer"}.intersection(fields):
                continue
            if feedback := saved_change_feedback(status, path, fields):
                status.defer_question(path, feedback)
            elif decode_change_delivery(fields).intents() or fields.get("kind") in ("verdict", "validation") or "feedback" in fields:
                status.defer_question(path, UnreadableDeliveryRecord())
        except FileNotFoundError:
            continue
        except Exception as error:
            _defer_question_error(status, path, path.name, error, delivery=UnreadableDeliveryRecord())


def saved_change_feedback(status: DeliveryStatus, path: Path, fields: dict[str, object]) -> MessageDelivery | None:
    question = decode_change_delivery(fields).question
    if not isinstance(question, OpenFeedback) or not isinstance(question.delivery, DeliveryIntent):
        return None
    intent = question.delivery
    saved = record_field(record_field(fields, "change_delivery"), intent.kind)
    if saved.get("attempt") != intent.attempt or "feedback_source" not in saved:
        return None
    source = record_field(saved, "feedback_source")
    listing = Path(cast(str, source["path"]))
    row = cast(str, source["row"])
    page = ReviewPage(listing, listing.parent, (row,), "")
    if not re.fullmatch(r"C\d+", row) or status.path(page, row) != path:
        raise ValueError("The saved Change feedback does not match its delivery path.")
    return MessageDelivery(page, row, tuple(map(staff.StaffId, migrated_recipients(saved) or ())),
                           cast(str, saved["message"]), intent.attempt,
                           tuple(map(staff.StaffId, migrated_recipients(source) or ())))


def recovery_listings(status: DeliveryStatus, paths: frozenset[Path]) -> tuple[Path, ...]:
    if not paths:
        return ()
    folder = status.root
    try:
        folder = change_review.lists_directory().resolve()
        return tuple(path for path in folder.iterdir() if path.match(change_review.PREFIX + "*.md"))
    except Exception as error:
        defer_unmatched_change_questions(status, paths)
        status.report_recovery_error(folder, error, "recovery: Cannot read the Change lists")
        return ()


def read_recovery_list(path: Path, *, include_reviewed: bool = True) -> change_review.ReviewListSnapshot:
    """Require the saved list before accepting its rows as evidence."""
    try:
        _ = path.stat()
        snapshot = change_review.review_snapshot(path, include_reviewed=include_reviewed)
        _ = path.stat()
    except FileNotFoundError as error:
        raise OSError(f"Cannot read the Change list {path}: {error}") from error
    return snapshot


def recover_change_messages(status: DeliveryStatus, paths: frozenset[Path], listings: Iterable[Path],
                            unreadable: set[Path], unready: set[Path],
                            only: change_review.PendingVerdictCleanup | change_review.NoVerdictCleanup, *,
                            deferred: bool = False) -> None:
    """Recover Change sends for the selected delivery records."""
    if not paths:
        return
    snapshots: dict[Path, change_review.ReviewListSnapshot] = {}
    incomplete = bool(unreadable)
    for listing in listings:
        if listing in unreadable:
            continue
        try:
            snapshots[listing] = read_recovery_list(listing)
        except Exception as error:
            incomplete = True
            status.report_recovery_error(listing, error, f"recovery {listing}")
    unmatched: set[Path] = set(paths) if incomplete else set()
    routing: change_routing.StaffRouting | None = None
    for listing, snapshot in snapshots.items():
        for row in (*snapshot.pending, *snapshot.reviewed):
            if isinstance(only, change_review.PendingVerdictCleanup) and row["#"] != only.row:
                continue
            path = change_routing.delivery_path(status.root, listing, row["#"])
            if path not in paths or path in unready:
                continue
            unmatched.discard(path)
            recovered_attempts: tuple[str, ...] = ()
            try:
                if path.is_symlink():
                    continue
                with status.locked():
                    if any(ownership.row_file == path for ownership in status.request_attempts.values()):
                        if deferred:
                            status.defer_question(path, UnreadableDeliveryRecord())
                        continue
                    fields = read_recovery_delivery(status, path)
                    decoded = decode_change_delivery(fields)
                    question = decoded.question
                    intents = decoded.intents()
                    if not intents or any(intent.attempt in status.request_attempts for intent in intents):
                        continue
                    if isinstance(decoded.verdict, DeliveryIntent) and not row.get("Verdict"):
                        row_id = row["#"]
                        current = read_recovery_list(listing)
                        row = next((record for record in current.reviewed if record["#"] == row_id), row)
                        if not row.get("Verdict"):
                            record_page = ReviewPage(listing, listing, (row_id,), "")
                            status.clear_locked(record_page, row_id, decoded, "verdict")
                            decoded = decoded.updated("verdict", NoDelivery())
                    intents = tuple(intent for intent in decoded.intents()
                                    if not row.get("Verdict") or intent.kind == "verdict")
                    recovered_attempts = tuple(intent.attempt for intent in intents)
                    for attempt in recovered_attempts:
                        status.request_attempts[attempt] = AttemptOwnership(path, "claimed")
                if not intents:
                    continue
                if routing is None:
                    routing = list_routing(snapshots)
                assignment = routing.owner_and_production(listing, row)
                owner = assignment.owner
                if isinstance(assignment.production, change_routing.UnavailableProduction):
                    raise ValueError("the production document is not available")
                page = AssignedReviewPage(listing, assignment.production, (row["#"],), "", owner)
                for intent in intents:
                    validation_question = intent.kind == "question" and decoded.validation_requested
                    recipients = intent.recipients
                    saved_question_route = (intent.kind == "question" and "recipients" in question_intent_fields(fields))
                    saved_change_route = intent.kind in ("verdict", "validation") and "recipients" in old_record(
                        fields.get(intent.kind))
                    progress = change_progress(fields, intent)
                    if progress is not None and progress.recipients is not None:
                        recipients = tuple(staff.StaffId(target) for target in progress.recipients)
                    elif not recipients and not (saved_question_route or saved_change_route):
                        recipients = (verdict_recipients(owner, row["Verdict"])
                                      if intent.kind == "verdict" else owner.showrunner_recipients)
                    question = decoded.question
                    if intent.kind == "question" and isinstance(question, OpenQuestion):
                        sender = question_sender(row, recipients) if validation_question else None
                        message = _request_message(page, row, question.question_id, question.text,
                                                   isinstance(question, OpenFeedback), sender=sender)
                    elif intent.kind == "validation":
                        message = validation_message(page, row, intent.attempt, intent.feedback)
                    else:
                        message = review_message(page, row, row["Verdict"], f"Merged: {row.get('Merged', '')}",
                                                 f"User feedback: {row['Verdict']}")
                    alerts = recipients if validation_question else owner.showrunner_recipients
                    deliver(MessageDelivery(page, row["#"], recipients, message, intent.attempt, alerts), status,
                            recovering_question=intent.kind == "question" and not isinstance(question, OpenFeedback))
            except FileNotFoundError:
                defer_unmatched_change_questions(status, (path,))
            except Exception as error:
                _defer_question_error(status, path, path.name, error, delivery=UnreadableDeliveryRecord())
            finally:
                try:
                    status.release_recovery_claims(recovered_attempts)
                except Exception as error:
                    _defer_question_error(status, path, path.name, error, delivery=UnreadableDeliveryRecord())
    defer_unmatched_change_questions(status, unmatched)


def expire_saved_questions(status: DeliveryStatus, paths: Iterable[Path], *,
                           retries: dict[Path, DeferredQuestion] | None = None) -> set[Path]:
    """Check every saved deadline before the recovery worker starts a send."""
    unreadable: set[Path] = set()
    for path in paths:
        delivery = retries[path] if retries is not None else UnreadableDeliveryRecord()
        try:
            if isinstance(delivery, ListFeedbackRetry) or path.is_symlink():
                continue
            fields = read_recovery_delivery(status, path)
            if "question_attempt" in fields:
                _ = prepare_page_recovery(status, path, decode_page_attempt(fields))
                continue
            current = read_question_progress(fields["question_delivery"]) if "question_delivery" in fields else None
            if current is None or current.state is QuestionDeliveryState.ANSWERED:
                continue
            with question_answer_lock(path, current.question_id, wait=False) as lock:
                if lock is file_lock.LockResult.BUSY:
                    status.defer_question(path, UnreadableDeliveryRecord())
                    continue
                with status.locked():
                    fields = read_recovery_delivery(status, path)
                    progress = read_question_progress(fields["question_delivery"]) if "question_delivery" in fields else None
                    if progress is None or progress != current:
                        continue
                    question = decode_change_delivery(fields).question
                    if not isinstance(question, OpenQuestion) or question.question_id != progress.question_id:
                        if not progress.state.pending:
                            continue
                        raise ValueError("Cannot read the saved Review question.")
                    if reconcile_native_answer(path, fields, question, progress) or not progress.state.pending:
                        continue
                    if progress.deadline is None:
                        progress, _ = progress.start_budget()
                    expired = progress.expire()
                    if expired != current:
                        write_review_progress(path, fields, expired)
                    if expired.state is QuestionDeliveryState.UNKNOWN:
                        status.defer_question(path, UnreadableDeliveryRecord())
        except StaleQuestionAttempt:
            continue
        except Exception as error:
            if isinstance(error, FileNotFoundError) and cast(object, error.filename) == str(path):
                continue
            unreadable.add(path)
            _defer_question_error(status, path, path.name, error, delivery=delivery)
    return unreadable


def recover_deferred_questions(status: DeliveryStatus) -> None:
    """Retry the saved attempts after the current sender releases its claim."""
    migrations = status.pending_migrations()
    migrated = {path for path in migrations if recover_delivery_migration(status, path)}
    if migrated or status.question_sources_pending.is_set():
        recover_question_sources(status)
    unreadable: set[Path] = set()
    cleanups = status.take_deferred_cleanups()
    cleanups.update(path.with_name(change_review.VERDICT_CLEANUP_PREFIX + path.name.removeprefix(DELIVERY_PREFIX))
                    for path in migrated)
    unready = recover_verdict_cleanups(status, tuple(sorted(cleanups)), unreadable)
    change_paths: set[Path] = set()
    pending = status.take_deferred_questions()
    failed = migrations - migrated
    failed |= {path for path, delivery in pending.items()
               if isinstance(delivery, UnreadableDeliveryRecord) and path not in migrations
               and not recover_delivery_migration(status, path)}
    for path in failed:
        _ = pending.pop(path, None)
    for path in migrated:
        _ = pending.setdefault(path, UnreadableDeliveryRecord())
    unreadable_deliveries = expire_saved_questions(status, pending, retries=pending)
    for path, delivery in pending.items():
        if path in unreadable_deliveries:
            continue
        if path in unready:
            status.defer_question(path, delivery)
            continue
        try:
            if delivery is None:
                recover_page_question(status, path, read_question_attempt(path))
            elif isinstance(delivery, ListFeedbackRetry):
                delivery.deliver()
            elif isinstance(delivery, UnreadableDeliveryRecord):
                fields = read_recovery_delivery(status, path)
                if "user_answer" in fields:
                    recover_question_answers(status, (path,))
                elif "question_attempt" in fields or "review_message" in fields:
                    recover_review_messages(status, (path,))
                elif feedback := saved_change_feedback(status, path, fields):
                    deliver(feedback, status)
                else:
                    change_paths.add(path)
            else:
                deliver(delivery, status)
        except Exception as error:
            if (not isinstance(delivery, ListFeedbackRetry) and isinstance(error, FileNotFoundError)
                    and cast(object, error.filename) == str(path)):
                continue
            _defer_question_error(status, path, path.name, error, delivery=delivery)
    if change_paths:
        paths = frozenset(change_paths)
        listings = recovery_listings(status, paths)
        recover_change_messages(status, paths, listings, unreadable, unready,
                                change_review.NO_VERDICT_CLEANUP, deferred=True)


def recover_review_messages(status: DeliveryStatus, paths: Iterable[Path] | None = None) -> None:
    """Restore Review messages through the shared claims and alerts."""
    for path in status.recovery_paths() if paths is None else paths:
        claimed: tuple[str,...] = ()
        is_question = False
        try:
            fields = read_recovery_delivery(status, path)
            if "question_attempt" in fields:
                is_question = True
                recover_page_question(status, path, decode_page_attempt(fields))
                continue
            with status.locked():
                if any(owner.row_file == path for owner in status.request_attempts.values()):
                    if paths is not None:
                        status.defer_question(path, UnreadableDeliveryRecord())
                    continue
                fields = read_recovery_delivery(status, path)
                stored = cast(StoredDeliverySlots, cast(object, fields))
                saved = stored.get('review_message')
                is_question = "question" in fields and saved is not None
                if saved is None:
                    continue
                if saved.get('submission_delivered'):
                    continue
                source = Path(saved['source'])
                row = saved.get('row',source.stem)
                if row.startswith((IMAGE_QUESTION_ROW_PREFIX, "List item ")):
                    continue
                page = ReviewMessagePage(source,source.parent,(row,),'',saved)
                slots = decode_change_delivery(stored)
                if isinstance(slots.question, OpenQuestion) and "question_delivery" in fields:
                    progress = read_question_progress(fields["question_delivery"])
                    if reconcile_native_answer(path, fields, slots.question, progress):
                        continue
                is_question = isinstance(slots.verdict, DeliveryIntent) and native_review_question(page, slots, slots.verdict)
                if not saved['saved']:
                    if not isinstance(slots.verdict, DeliveryIntent):
                        continue
                    if status.has_review_save_receipt(page, row, slots.verdict):
                        page = replace(page, saved={**saved, 'saved': True})
                    elif not is_question:
                        print(f'review-links recovery {path.name}: review save state unknown; message kept',
                              file=sys.stderr)
                        continue
                claimed = tuple(intent.attempt for intent in slots.intents())
                for attempt in claimed:
                    status.request_attempts[attempt] = AttemptOwnership(path,'claimed')
            for intent in slots.intents():
                question_send = native_review_question(page, slots, intent)
                owner = staff.StaffId(saved["owner"])
                recipients = intent.recipients
                if question_send and not recipients and "recipients" not in old_record(stored.get("verdict")):
                    recipients = review_alert_showrunners(owner)
                deliver(MessageDelivery(page, row, recipients, saved["message"], intent.attempt, (owner,)), status,
                        recovering_question=question_send)
        except FileNotFoundError:
            continue
        except (OSError, ValueError, KeyError, TypeError, AttributeError, update_registration.RegistrationFailure,
                update_registration.InputError) as error:
            _defer_question_error(status, path, path.name, error, delivery=UnreadableDeliveryRecord())
        finally:
            status.release_recovery_claims(claimed)


def recover_question_answers(status: DeliveryStatus, paths: Iterable[Path] | None = None) -> None:
    """Recover saved user answers through the shared claims and deadline."""
    for path in status.recovery_paths() if paths is None else paths:
        claimed: tuple[str, ...] = ()
        is_question = False
        try:
            with status.locked():
                if any(owner.row_file == path for owner in status.request_attempts.values()):
                    if paths is not None:
                        status.defer_question(path, UnreadableDeliveryRecord())
                    continue
                fields = cast(StoredDeliverySlots, cast(object, read_recovery_delivery(status, path)))
                saved = fields.get("user_answer")
                if saved is None:
                    continue
                if saved.get("kind") == "followup":
                    status.defer_question(path, UnreadableDeliveryRecord())
                    continue
                is_question = saved.get("kind") == "question"
                slots = delivery_state.decode_value(fields)
                if is_question and isinstance(slots.question, OpenQuestion) and "question_delivery" in fields:
                    native_fields = cast(dict[str, object], cast(object, fields))
                    progress = read_question_progress(native_fields["question_delivery"])
                    if reconcile_native_answer(path, native_fields, slots.question, progress):
                        continue
                intents = slots.intents()
                if not intents:
                    continue
                page = UserQuestionPage(Path(saved["source"]), Path(saved["production"]),
                                        (Path(saved["source"]).stem,), "", saved)
                try:
                    question = saved_user_question(page)
                except OSError as error:
                    # The strict reader wraps errors from the source file.
                    if not isinstance(error.__cause__, FileNotFoundError):
                        raise
                    question = None
                # An answer closes its source. Other actions leave it open.
                if question is None or question.status != ("closed" if saved.get("kind", "answer") in ("answer", "feedback") else "open"):
                    status.clear_locked(page, page.rows[0], slots, "verdict")
                    continue
                if not saved.get("saved", False):
                    if not status.has_review_save_receipt(page, page.rows[0], slots.verdict):
                        if not is_question:
                            status.clear_locked(page, page.rows[0], slots, "verdict")
                            print(f"review-links recovery {path.name}: answer save state unknown; not sent", file=sys.stderr)
                            continue
                    else:
                        page = replace(page, saved={**saved, "saved": True})
                claimed = tuple(intent.attempt for intent in intents)
                for attempt in claimed:
                    status.request_attempts[attempt] = AttemptOwnership(path, "claimed")
            for intent in intents:
                question_send = native_review_question(page, slots, intent)
                owner = staff.StaffId(saved["owner"])
                recipients = intent.recipients
                if question_send and not recipients and "recipients" not in old_record(fields.get("verdict")):
                    recipients = review_alert_showrunners(owner)
                deliver(MessageDelivery(page, page.rows[0], recipients, saved["message"], intent.attempt, (owner,)),
                        status, recovering_question=question_send)
        except FileNotFoundError:
            continue
        except (OSError, ValueError, KeyError, TypeError, AttributeError, update_registration.RegistrationFailure,
                update_registration.InputError) as error:
            _defer_question_error(status, path, path.name, error, delivery=UnreadableDeliveryRecord())
        finally:
            status.release_recovery_claims(claimed)


def console_folder(root: Path) -> Path:
    """Create the fixed console folder and its browser token once; later calls keep both."""
    folder = root / CONSOLE_FOLDER
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = folder / BROWSER_MANIFEST
    if target.exists():
        return folder
    manifest: BrowserManifest = {"token": secrets.token_urlsafe(), "initial": ""}
    draft = folder / f"{BROWSER_MANIFEST}.{secrets.token_hex(8)}"
    atomic_file.write_bytes_atomic(draft, json.dumps(manifest).encode(), mode=0o600)
    try:
        # A link never replaces a token that a concurrent first request created.
        os.link(draft, target)
    except FileExistsError:
        pass
    finally:
        draft.unlink()
    return folder


def console_url() -> str:
    """Return the fixed console address; the server renders it from the current code at each request."""
    return f"http://{review_link.link_host()}:{conf.read('review_links').whole('server_port')}/{CONSOLE_FOLDER}/"


def page_url() -> str:
    """Return the fixed Changes page path under the review root."""
    _ = console_folder(review_link.review_root().resolve())
    return f"/{CONSOLE_FOLDER}/{PAGE_NAME}"


def questions_url() -> str:
    """Return the Questions page: the same browser, limited to questions."""
    return page_url() + "?type=questions"


def rebuilds_url() -> str:
    """Return the open rebuild needs beside the console's browser."""
    return page_url().rsplit("/", 1)[0] + "/" + REBUILDS_NAME


def reply_command(key: str, question: str = '') -> str:
    machine = ' --machine '+shlex.quote(remote.local_machine()) if key.startswith(new_item_dialog.KEY_PREFIX) else ''
    return ('~/.claude/scripts/lib/py ~/.claude/scripts/review_links/change_page.py answer-question '
            + '--item '+shlex.quote(key)+(' --question '+shlex.quote(question) if question else '')+machine
            + " --text '<your answer>'")


def answer_item(key: str, words: str, question_id: str, status: DeliveryStatus) -> bool:
    from scripts.review_links import decision_store, efficiency_reviews, review_items, review_submissions
    if key.startswith(review_submissions.KEY_PREFIX):
        try:
            return review_submissions.answer(key, words, question_id, root=status.root)
        except review_submissions.SubmissionError as error:
            raise question_record.AnswerRefused(str(error)) from error
    item = next((item for item in review_items.read(status.root) if key and key in (item.key,item.id)),None)
    if item is None:
        raise question_record.AnswerRefused('the Review item does not exist')
    source = item.source
    who = review_items.name(item.owner,staff.snapshot())
    if isinstance(source, (review_items.QuestionSource, review_items.InvestigationSource,
                           review_items.ProposalSource, review_items.EfficiencySource)):
        if isinstance(source, (review_items.QuestionSource, review_items.ProposalSource)):
            path, row = source.record.path, source.record.path.stem
        elif isinstance(source, review_items.InvestigationSource):
            path, row = source.path, source.record.id
        else:
            path, row = efficiency_reviews.path(source.path), decision_store.efficiency_identity(item)
        page = status.metadata_page(ReviewPage(path, path.parent, (row,), ''))
        row = page.rows[0]
        delivery_path = status.path(page, row)
        with status.native_action(page, row):
            with status.locked():
                slots = status.slots(page, row)
                tracked = not isinstance(slots.question, NoQuestion) or bool(question_id)
                if tracked:
                    question = status.question_for_reply(page, row, question_id, words.strip())
                    if isinstance(question, AnsweredQuestion):
                        return False
            if isinstance(source, review_items.QuestionSource):
                identities = update_registration.QuestionStaff.read()
                update_registration.reply_record(source.record,words,who,identities=identities,question_id=question_id)
                changed = True
            elif isinstance(source, review_items.InvestigationSource):
                _ = investigation.answer(source.record.owner,source.record.id,words,question_id=question_id)
                changed = True
            else:
                try:
                    changed = decision_store.answer(item,words,who)
                except decision_store.NoOpenQuestion as error:
                    raise question_record.AnswerRefused(str(error)) from error
            if tracked:
                with status.locked():
                    delivery_state.complete_review_reply(delivery_path, question_id, words.strip())
            return changed
    if isinstance(source,review_items.ChangeSource):
        assert isinstance(source.assignment.production,Path)
        page = ReviewPage(source.path,source.assignment.production,(source.row['#'],),'')
        changed = status.answer_question(page,source.row['#'],question_id,words)
        if changed:
            change_review.record_review_log(source.path,source.row['#'],'Answer: '+words,page.production,who)
        return changed
    raise question_record.AnswerRefused('this item cannot receive an answer')


def _forward_reply(machine: str, key: str, words: str, token: str) -> int:
    arguments = shlex.join(["answer-question", "--item=" + key, "--question=" + token, "--text=" + words])
    command = '"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/review_links/change_page.py" ' + arguments
    result = subprocess.run(["ssh", "-o", "BatchMode=yes", machine, command], stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=30, check=False)
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    return result.returncode


def main(arguments: list[str]) -> int:
    if arguments and arguments[0] == "answer-question":
        parser = argparse.ArgumentParser(description="Answer an Ask question action on a Review item.")
        _ = parser.add_argument("--item", default="")
        _ = parser.add_argument("--production")
        _ = parser.add_argument("--list", type=Path)
        _ = parser.add_argument("--row")
        _ = parser.add_argument("--question", default="")
        _ = parser.add_argument("--machine", default="")
        _ = parser.add_argument("--text", required=True)
        args = parser.parse_args(arguments[1:])
        try:
            key, words, token = cast(str, args.item), cast(str, args.text), cast(str, args.question)
            machine = cast(str, args.machine)
            scoped = key.startswith(new_item_dialog.KEY_PREFIX)
            if machine:
                if not scoped:
                    raise ValueError("Use --machine with a sent item's full key.")
                if machine != remote.local_machine():
                    host = remote.machine_alias(machine)
                    if isinstance(host, remote.MachineNameRefused):
                        raise ValueError(host.reason)
                    if host.alias != remote.this_machine():
                        return _forward_reply(host.alias, key, words, token)
            if key:
                retained = scoped and not staff.is_owner()
                with staff.supplied_snapshot(staff.local_snapshot()) if retained else nullcontext():
                    changed = answer_item(key, words, token, DeliveryStatus(review_link.review_root()))
                print(f'Answered {key}' if changed else f'{key} already has this answer')
                return 0
            if not all((cast(str | None,args.production),cast(Path | None,args.list),cast(str | None,args.row),cast(str,args.question))):
                parser.error('pass --item, or --production, --list, --row and --question')
            target = change_review.resolve_row(cast(str, args.row), production=cast(str, args.production),
                                               review_list=cast(Path, args.list))
            shown = change_review.row_display_id(target.path, target.row)
            page = ReviewPage(target.path, change_review.production_doc(cast(str, args.production)), (target.row,), "")
            changed = DeliveryStatus(review_link.review_root()).answer_question(
                page, page.rows[0], cast(str, args.question), cast(str, args.text))
        except (OSError, ValueError, change_review.ChangeReviewError, Refusal, subprocess.SubprocessError,
                update_registration.RegistrationFailure) as error:
            print(f"change page: {error}", file=sys.stderr)
            return 1
        print(f"Answered {shown}" if changed else f"{shown} already has this answer")
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("rows", nargs="*")
    _ = parser.add_argument("--production")
    _ = parser.add_argument("--list", type=Path)
    args = parser.parse_args(arguments)
    rows = cast(list[str], args.rows)
    production = cast(str | None, args.production)
    review_list = cast(Path | None, args.list)
    try:
        if not rows:
            print(console_url())
            return 0
        if production is None:
            raise ValueError("pass --production for an older selected-row link")
        doc = change_review.production_doc(production)
        targets = [change_review.resolve_row(row, production=production, review_list=review_list) for row in rows]
        if any(target.path.resolve() != targets[0].path.resolve() for target in targets):
            raise ValueError("selected rows must belong to one review list")
        print(publish(doc, targets[0].path, [target.row for target in targets]))
    except (OSError, ValueError, change_review.ChangeReviewError, subprocess.SubprocessError, Refusal) as error:
        print(f"change page: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

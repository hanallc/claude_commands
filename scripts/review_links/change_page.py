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
from contextlib import ExitStack, contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import IO, Literal, NotRequired, TypedDict, cast, overload
from urllib.parse import parse_qs, urlsplit
from weakref import WeakSet, WeakValueDictionary, finalize

from scripts.console_text import check as console_text

from scripts.agents import remote
from scripts.lib import build_folders, process_start
from scripts.lib.boot_identity import boot_identity
from scripts.lib.file_lock import LockResult
from scripts.message import send_result
from scripts.production import change_review as change_review
from scripts.production import investigation, proposal_issue, unit_lookup, record_store
from scripts.production.issues import Issue
from scripts.production import update_registration
from scripts.production import unrouted_alert, change_routing, delivery_state, next_item, showrunners, staff
from scripts.production.delivery_state import (
    NoSavedListKeeper, NoQuestionSendOwner, NoQuestionSendReceipt, QuestionAttempt, QuestionAttemptMissing,
    QuestionSendOwner, QuestionSendReceipt, QuestionSendState, QuestionSource,
    AnsweredQuestion as AnsweredQuestion, DeliveryIntent as DeliveryIntent, DeliverySlots as DeliverySlots,
    NoDelivery as NoDelivery, NoQuestion as NoQuestion, OpenQuestion as OpenQuestion,
    ValidationRequested as ValidationRequested,
)
from scripts.lib import atomic_file, conf, urgency
from scripts.lib.user_zone import user_zone
from scripts.production.production_doc import Refusal as Refusal, production_field, read_production
from scripts.feature_log import feature_log
from scripts.review_links.question_path import AnswerTime, NO_ANSWER_TIME, answer_log_id
from scripts.review_links import (console_theme, image_store, new_item_dialog, page_frame, question_record, review_events,
                                  reply_tokens, review_link, staff_names)
from scripts.review_links.question_record import AnswerRefused as AnswerRefused
from scripts.review_links.review_items import (
    CompletedFeedback as CompletedFeedback, OpenFeedback as OpenFeedback,
    change_on_list, decode_change_delivery, read_change_delivery, validation_recipients, validation_request_words,
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
IMAGE_QUESTION_ROW_PREFIX = "Images "


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
    recipients: list[str]


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
    answered_at: str
    waiting_why: str
    validation: StoredValidationIntent | StoredValidationRequested
    verdict: StoredDeliveryIntent
    question: StoredOpenQuestion | StoredAnsweredQuestion
    user_answer: StoredUserAnswer
    review_message: StoredReviewMessage


WITH_SHOWRUNNER = "This change is already with the showrunner."
CHANGE_HANDOFFS = frozenset(("validate", "question", "feedback"))
VALIDATION_QUESTION = "validation-question"


def saved_delivery_times(path: Path) -> dict[str, str]:
    """Read the saved handoff and answer times."""
    try:
        fields = delivery_state.read_fields(path)
    except (FileNotFoundError, ValueError):
        return {}
    return {key: value for key in ("handed_at", "answered_at")
            if isinstance(value := fields.get(key), str)}


def read_handed_at(path: Path) -> str | None:
    return saved_delivery_times(path).get("handed_at")


def require_user_turn(slots: DeliverySlots) -> None:
    if not slots.waits_for_user(False):
        raise ChangeHandoffRefused(WITH_SHOWRUNNER)


ImageQuestionDispatchState = Literal["started", "started_after_unconfirmed", "accepted"]


class ImageQuestionDispatch(TypedDict):
    attempt: str
    state: ImageQuestionDispatchState


@dataclass(frozen=True)
class NoImageQuestionDispatch:
    """The record has no send mark for this image question."""


class ScopedReplyCommand(TypedDict):
    key: str
    token_sha256: str


class StoredReviewMessage(TypedDict):
    saved: bool
    source: str
    owner: str
    message: str
    # A record saved before efficiencies has no row; its row is the source's stem.
    row: NotRequired[str]
    image_question_dispatch: NotRequired[ImageQuestionDispatch]
    submission_request: NotRequired[str]
    submission_delivered: NotRequired[bool]
    scoped_reply: NotRequired[ScopedReplyCommand]


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


class StoredReviewDispatch(TypedDict):
    attempt: str
    state: Literal["started", "finished"]
    keyed: NotRequired[Literal[True]]


class StoredQuestionDispatch(TypedDict):
    attempt: str
    state: Literal["started", "finished", "failed", "unknown"]
    question_id: NotRequired[str]
    error: NotRequired[str]


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


@dataclass(frozen=True)
class DeliveryConfirmed:
    """The transport accepted the message."""


@dataclass(frozen=True)
class DeliveryUnconfirmed:
    """This call has no confirmed transport result."""


type DeliveryOutcome = DeliveryConfirmed | FailedDelivery | DeliveryUnconfirmed


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
    scoped_reply: NotRequired[ScopedReplyCommand]


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


type ChangeWorkFiling = Callable[[ReviewPage, dict[str, str], change_routing.ChangeAssignment, str, str],
                                 investigation.ListDestination | investigation.NoListDestination]


_change_work_filing: ContextVar[ChangeWorkFiling] = ContextVar(
    "change_work_filing", default=lambda _page, _row, _assignment, _outcome, _words:
    investigation.NoListDestination.ABSENT)


@contextmanager
def file_change_actions(filing: ChangeWorkFiling) -> Generator[None]:
    """Use the same List filing path for live change forms."""
    token = _change_work_filing.set(filing)
    try:
        yield
    finally:
        _change_work_filing.reset(token)


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
            if 'scoped_reply' not in self.saved:
                self.saved['message'] = self.saved['message'].replace(reply_command(key), reply_command(key, identity))


@dataclass(frozen=True)
class ReviewMessagePage(ReviewPage):
    """A Review record and its saved message use the existing delivery slots."""

    saved: StoredReviewMessage


class QuestionAnswerRefused(question_record.AnswerRefused):
    """The browser can show this refusal to the user."""


class ChangeHandoffRefused(QuestionAnswerRefused):
    """The change cannot accept this action in its current state."""


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

_ANSWER_COMMAND_LINE = "\nAnswer with this command: "
_LIST_ANSWER_PREFIX = "~/.claude/scripts/lib/py ~/.claude/scripts/production/the_list.py answer "
_LIST_ANSWER_SUFFIX = ' --question {question_id} --text "<answer>"'


@dataclass(frozen=True)
class SavedQuestionDelivery:
    source: QuestionSource
    recipient: staff.StaffId
    title: str
    answer_command: str
    alert_owner: staff.StaffId | None = None

    @property
    def heading(self) -> str:
        return f"{self.source.row.rsplit('/', 1)[0]}: Ask question"

    @property
    def message_body(self) -> str:
        return self._body(self.answer_command)

    def _body(self, command: str) -> str:
        return f"{self.source.words}{_ANSWER_COMMAND_LINE}{command}"

    @property
    def message(self) -> str:
        return f"{self.heading}\n{self.title}\n{self.message_body}"

    def page(self) -> ReviewPage:
        path = Path(self.source.path)
        return ReviewPage(path, path.parent, (self.source.row,), "")

    def matches(self, record: QuestionAttempt) -> bool:
        return (self.source.matches_attempt(record.source) and record.recipient_id == self.recipient
                and self.matches_message(record.message))

    def matches_message(self, message: str) -> bool:
        if not self.source.row.startswith("List item "):
            return message == self.message
        parts = message.split("\n", 2)
        if len(parts) != 3:
            return False
        heading, title, body = parts
        # Attempts saved before the keeper flag carry the List answer command without --keeper.
        before_keeper = self._body(re.sub(r" --keeper \S+", "", self.answer_command, count=1))
        return (heading == self.heading and bool(title.strip()) and title.splitlines() == [title]
                and body in (self.message_body, before_keeper))


@dataclass(frozen=True)
class QuestionDeliveryTarget:
    path: Path
    item: str
    recipient: staff.StaffId
    row: str
    title: str
    answer_command: str
    alert_owner: staff.StaffId | None = None
    initial_recipient: question_record.QuestionRecipient = question_record.DefaultQuestionRecipient.PAGE
    keeper: staff.StaffId | NoSavedListKeeper = NoSavedListKeeper()

    def saved(self, question: question_record.Question) -> SavedQuestionDelivery:
        row = f"{self.row}/{question.question_id}"
        source = QuestionSource(str(self.path.resolve()), self.item, question.question_id, question.words, row, self.keeper)
        command = self.answer_command.format(item=shlex.quote(self.item), question_id=question.question_id)
        return SavedQuestionDelivery(source, self.recipient, self.title, command, self.alert_owner)


def list_question_target(path: Path, item: str, number: int, title: str,
                         recipient: staff.StaffId, *, keeper: staff.StaffId,
                         alert_owner: staff.StaffId | None = None) -> QuestionDeliveryTarget:
    # The command names the record's folder, so a recipient never answers a same-number record in its own folder.
    command = _LIST_ANSWER_PREFIX + f"{{item}} --keeper {shlex.quote(keeper)}" + _LIST_ANSWER_SUFFIX
    return QuestionDeliveryTarget(path, item, recipient, f"List item {number}", title, command, alert_owner, keeper=keeper)


def image_question_target(path: Path, image: image_store.ImageRecord) -> QuestionDeliveryTarget:
    command = ('~/.claude/scripts/lib/py ~/.claude/scripts/review_links/images.py answer '
               + '--id {item} --question {question_id} --text "<answer>"')
    owner = staff.StaffId(image["owner"])
    summary = "\n".join((image["title"], *image_store.look_for_message_lines(image)))
    return QuestionDeliveryTarget(path, image["id"], owner, IMAGE_QUESTION_ROW_PREFIX + image["id"],
                                  summary, command, owner)


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


def _question_lock_path(path: Path, name: str) -> Path:
    return path.parent / ".review-link-question-jobs" / name


type QuestionLockAcquisition = IO[str] | Literal[LockResult.BUSY]


def _take_question_attempt_lock(path: Path) -> QuestionLockAcquisition:
    path.parent.mkdir(exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException as error:
        handle.close()
        if isinstance(error, BlockingIOError):
            return LockResult.BUSY
        raise
    return handle


def _take_question_recovery_locks(locks: ExitStack, path: Path, record: QuestionAttempt) -> QuestionLockAcquisition:
    """Take every sender lock and return the attempt lock for the claim."""
    names = [path.name, record.attempt_id]
    if isinstance(record.owner, QuestionSendOwner):
        names.append(record.owner.job_id)
    handles: dict[str, IO[str]] = {}
    for name in dict.fromkeys(names):
        handle = _take_question_attempt_lock(_question_lock_path(path, name))
        if handle is LockResult.BUSY:
            return handle
        handles[name] = locks.enter_context(handle)
    return handles[record.attempt_id]


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


def read_question_attempt(path: Path) -> QuestionAttempt:
    return delivery_state.decode_question_attempt(delivery_state.read_fields(path))


@dataclass(frozen=True)
class NoQuestionAttempt:
    """The delivery file has no valid current attempt."""


@dataclass(frozen=True)
class QuestionSendPermitted:
    state: Literal[QuestionSendState.PREPARED, QuestionSendState.READY, QuestionSendState.FAILED]
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
    attempt: QuestionAttempt
    state: Literal[QuestionSendState.FAILED] = QuestionSendState.FAILED


type QuestionDeliveryView = (QuestionSendPermitted | ResendBlocked | QuestionDeliveryUnknown
                             | QuestionDeliveryAccepted | QuestionDeliveryFailed)


_QUESTION_SENDER_UNVERIFIED = "The server cannot verify the question sender."
_QUESTION_SENDER_ACTIVE = "The previous question sender is still active."


def _legacy(status: DeliveryStatus, page: ReviewPage, saved: SavedQuestionDelivery,
            fields: dict[str, object]) -> QuestionSendState:
    row = saved.source.row
    try:
        failure_fields = delivery_state.read_fields(status.closeout_path(page, row))
    except FileNotFoundError:
        failure_fields = {}
    except (OSError, ValueError):
        failure_fields = None
    return delivery_state.legacy_question_state(
        fields, failure_fields, partial(status.owns_live_claim, page, row), saved.source, saved.recipient,
        saved.matches_message)


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
    lock = observe_job(_question_lock_path(path, path.name))
    record: QuestionAttempt | NoQuestionAttempt = NoQuestionAttempt()
    owner: QuestionSendOwner | NoQuestionSendOwner = NoQuestionSendOwner()
    try:
        fields = delivery_state.read_fields(path)
        try:
            record = delivery_state.decode_question_attempt(fields)
        except QuestionAttemptMissing:
            state = _legacy(status, page, saved, fields)
        else:
            same_question = saved.source.matches_attempt(record.source) and record.recipient_id == saved.recipient
            failed = same_question and record.state is QuestionSendState.FAILED
            matches = saved.matches(record)
            state = record.state if failed or matches else QuestionSendState.UNKNOWN
            if failed and not matches:
                return QuestionDeliveryFailed(record)
            owner = record.owner
            if lock in ("released", "unobserved"):
                lock = observe_job(_question_lock_path(path, record.attempt_id))
            if (lock in ("released", "unobserved") and isinstance(owner, QuestionSendOwner)
                    and owner.job_id != record.attempt_id):
                lock = observe_job(_question_lock_path(path, owner.job_id))
    except (FileNotFoundError, ValueError):
        state = QuestionSendState.UNKNOWN
    except OSError:
        return ResendBlocked("The server cannot read the question delivery record.", record)
    if state in (QuestionSendState.SENT, QuestionSendState.QUEUED):
        return QuestionDeliveryAccepted(state, record)
    if lock == "unreadable":
        return ResendBlocked(_QUESTION_SENDER_UNVERIFIED, record)
    if lock == "held":
        if (state is QuestionSendState.CLAIMED or state is QuestionSendState.SENDING) and isinstance(owner, QuestionSendOwner):
            if _question_owner_matches(owner):
                return ResendBlocked(_QUESTION_SENDER_ACTIVE, record, state)
            return ResendBlocked(_QUESTION_SENDER_UNVERIFIED, record)
        return ResendBlocked(_QUESTION_SENDER_ACTIVE, record)
    if state is QuestionSendState.CLAIMED and isinstance(record, NoQuestionAttempt):
        return ResendBlocked(_QUESTION_SENDER_ACTIVE, record, state)
    if state in (QuestionSendState.CLAIMED, QuestionSendState.READY):
        return QuestionSendPermitted(QuestionSendState.READY, record)
    if state is QuestionSendState.PREPARED or state is QuestionSendState.FAILED:
        return QuestionSendPermitted(state, record)
    return QuestionDeliveryUnknown(record)


def publish_question_attempt(path: Path, record: QuestionAttempt) -> None:
    data = {**asdict(record), "schema": 2, "message_id": record.message_id}
    _ = data.pop("resend_requests", None)
    if isinstance(record.source.keeper, NoSavedListKeeper):
        _ = cast(dict[str, object], data["source"]).pop("keeper")
    if isinstance(record.owner, NoQuestionSendOwner):
        data["owner"] = None
    if isinstance(record.receipt, NoQuestionSendReceipt):
        data["receipt"] = None
    try:
        atomic_file.write_text_atomic(path, json.dumps({"question_attempt": data}) + "\n", new_file_mode=0o600)
    except atomic_file.AtomicDurabilityUnconfirmed:
        if record.state is QuestionSendState.SENDING or read_question_attempt(path) != record:
            raise


class StaleQuestionAttempt(ValueError):
    """The attempt no longer has the record that authorized the next write."""


class ClosedQuestionSource(ValueError):
    """The question is closed, answered, or absent."""


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
    publish_question_attempt(path, replace(record, revision=expected.revision + 1, resend_requests=()))


def question_envelope(page: ReviewPage, record: QuestionAttempt,
              job: QuestionJob | NoQuestionJob = NO_QUESTION_JOB) -> PageQuestionSend:
    return PageQuestionSend(page, record.source.row, (record.recipient_id,), record.message,
                            record.attempt_id, (record.recipient_id,), job=job)


def claim_question(status: DeliveryStatus, delivery: VerifiedQuestionAttempt,
                   *, lock: IO[str] | None = None) -> PageQuestionSend:
    record = delivery.record
    page = delivery.saved.page()
    path = status.path(page, delivery.saved.source.row)
    lock_path = _question_lock_path(path, record.attempt_id)
    claimed = record
    try:
        start = process_start.process_start(os.getpid())
        boot_id = _boot_id()
        if not isinstance(start, process_start.Started) or not start.start.strip() or not boot_id.strip():
            raise OSError("Cannot read this question sender's process identity.")
        owner = QuestionSendOwner(socket.gethostname(), boot_id, os.getpid(), start.start, record.attempt_id)
        if lock is None:
            acquired = _take_question_attempt_lock(lock_path)
            if acquired is LockResult.BUSY:
                status.defer_question(path)
                return question_envelope(page, record)
            lock = acquired
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
        _finish_unstarted_question(status, path, error, record, claimed)
        return question_envelope(page, record)


def ready_and_claim_question(status: DeliveryStatus, path: Path, record: QuestionAttempt,
                             saved: SavedQuestionDelivery) -> PageQuestionSend:
    try:
        delivery = ready_question(path, record, saved)
    except Exception as error:
        _finish_unstarted_question(status, path, error, record)
        return question_envelope(saved.page(), record)
    return claim_question(status, delivery)


def ready_question(path: Path, record: QuestionAttempt, saved: SavedQuestionDelivery) -> VerifiedQuestionAttempt:
    if not saved.matches(record):
        raise question_record.AnswerRefused("The saved question does not match this delivery.")
    ready = replace(record, revision=record.revision + 1, state=QuestionSendState.READY,
                    owner=NoQuestionSendOwner(), receipt=NoQuestionSendReceipt(), error="", resend_requests=(),
                    earlier_unconfirmed=record.earlier_unconfirmed or record.state in (
                        QuestionSendState.SENDING, QuestionSendState.UNKNOWN))
    advance_question_attempt(path, record, ready)
    return VerifiedQuestionAttempt(ready, saved)


def send_question(recipient: staff.StaffId, message: str, message_id: str
                  ) -> tuple[send_result.ConfirmedSendOutcome | send_result.UnconfirmedSendOutcome, str]:
    """Pass the same message ID to the transport on every attempt."""
    member = staff.snapshot().by_id(recipient)
    if member is None:
        return send_result.ConfirmedSendOutcome(send_result.OutcomeHeader("refused", "No staff record.", "")), "No staff record."
    return unrouted_alert.message_outcome(f"session:{member.current_session}", SENDER, message,
                                          *review_send_options(message_id, str(recipient)))


def _send_result(delivery: VerifiedQuestionAttempt) -> QuestionAttempt:
    record, saved = delivery.record, delivery.saved
    result = replace(record, owner=NoQuestionSendOwner())
    try:
        outcome, error = send_question(saved.recipient, record.message, saved.source.message_id(saved.recipient))
    except Exception as failure:
        return replace(result, state=QuestionSendState.FAILED, error=str(failure))
    if isinstance(outcome, send_result.ConfirmedSendOutcome):
        header = outcome.header
        if header.outcome in ("sent", "kept", "queued", "skipped"):
            receipt = QuestionSendReceipt("kept" if header.outcome == "skipped" else header.outcome, header.detail)
            state = QuestionSendState.QUEUED if receipt.outcome == "queued" else QuestionSendState.SENT
            return replace(result, state=state, receipt=receipt)
        if header.outcome in ("failed", "refused", "replaced"):
            return replace(result, state=QuestionSendState.FAILED, error=error)
    return replace(result, state=QuestionSendState.UNKNOWN, error=error)


def _list_question_store(record: QuestionAttempt) -> record_store.NumberedRecords:
    source = record.source
    path = Path(source.path)
    keeper = source.keeper
    if isinstance(keeper, NoSavedListKeeper):
        keeper = _legacy_list_command_keeper(source, record.message.rpartition(_ANSWER_COMMAND_LINE)[2])
    owner = record.recipient_id if isinstance(keeper, NoSavedListKeeper) else keeper
    listing = record_store.NumberedRecords(owner, "list", "L", "list item", ValueError)
    try:
        if listing.path(source.item_id).resolve() == path:
            return listing
        if isinstance(keeper, NoSavedListKeeper):
            return _legacy_list_question_store(listing, source)
    except ValueError as error:
        raise StaleQuestionAttempt("The List question source is unavailable.") from error
    raise StaleQuestionAttempt("The List question source has moved.")


def _legacy_list_command_keeper(source: QuestionSource, command: str) -> staff.StaffId | NoSavedListKeeper:
    prefix = _LIST_ANSWER_PREFIX + shlex.quote(source.item_id)
    suffix = _LIST_ANSWER_SUFFIX.format(question_id=source.question_id)
    if not command.startswith(prefix) or not command.endswith(suffix):
        raise ValueError("The saved List answer command does not match this question.")
    option = command[len(prefix):-len(suffix)]
    if not option:
        return NoSavedListKeeper()
    words = shlex.split(option)
    if len(words) != 2 or option != " --keeper " + shlex.quote(words[1]):
        raise ValueError("The saved List answer command has an invalid keeper.")
    return staff.StaffId(words[1])


def _legacy_list_question_store(listing: record_store.NumberedRecords,
                                source: QuestionSource) -> record_store.NumberedRecords:
    path = Path(source.path)
    folder = path.parent
    if folder.name == listing.collection and folder.parent.parent == record_store.state_root().resolve():
        inferred = replace(listing, owner=staff.StaffId(folder.parent.name))
        if inferred.path(source.item_id).resolve() == path:
            return inferred
    keepers = record_store.morty_ids(staff.snapshot())
    if len(keepers) != 1:
        raise ValueError("The List keeper is unavailable.")
    morty = replace(listing, owner=keepers[0])
    if morty.path(source.item_id).resolve() != path:
        raise ValueError("The List question source has moved.")
    return morty


def _open_list_question(listing: record_store.NumberedRecords,
                        record: QuestionAttempt) -> tuple[dict[str, str], question_record.Question]:
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
    if fields["state"] == "Closed":
        raise ClosedQuestionSource("The List question source is Closed.")
    questions = delivery_state.read_list_questions(fields.get("questions", "[]"))
    question = _verify_open_question(record, questions)
    if question.recipient_id(listing.owner) != record.recipient_id:
        raise StaleQuestionAttempt("The List question has a different recipient.")
    return fields, question


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
        _ = _open_list_question(listing, record)
        yield


def _verify_open_question(record: QuestionAttempt, questions: tuple[question_record.Question, ...]) -> question_record.Question:
    question = next((question for question in questions if question.question_id == record.source.question_id), None)
    if question is None or question.answer is not None:
        raise ClosedQuestionSource("The question is no longer open.")
    if question.words != record.source.words:
        raise ValueError("The saved question has different words.")
    return question


def _authorize_question_send(status: DeliveryStatus, path: Path, record: QuestionAttempt,
                             sending: QuestionAttempt) -> None:
    with _lock_open_question_source(status, record), status.locked():
        if record.state is QuestionSendState.CLAIMED:
            advance_question_attempt(path, record, sending)
        else:
            _require_current_question_attempt(path, record)


def _defer_question_error(status: DeliveryStatus, path: Path, row: str, error: Exception, *,
                          delivery: MessageDelivery | None = None) -> None:
    status.defer_question(path, delivery)
    print(f"review-links question {row}: " + " ".join(str(error).splitlines()), file=sys.stderr)


def _finish_unstarted_question(status: DeliveryStatus, path: Path, error: Exception,
                               record: QuestionAttempt, *alternatives: QuestionAttempt) -> None:
    """Record a failure or keep the attempt for recovery."""
    closed = isinstance(error, ClosedQuestionSource)
    try:
        current = read_question_attempt(path)
        if current not in (record, *alternatives):
            return
        unconfirmed = not closed and (current.earlier_unconfirmed or current.state is QuestionSendState.UNKNOWN)
        state = QuestionSendState.UNKNOWN if closed or unconfirmed else QuestionSendState.FAILED
        advance_question_attempt(path, current, replace(current, state=state, owner=NoQuestionSendOwner(),
                                                        earlier_unconfirmed=unconfirmed,
                                                        error="" if closed else str(error)))
    except (FileNotFoundError, StaleQuestionAttempt):
        return
    except (OSError, ValueError) as failure:
        _defer_question_error(status, path, record.source.row, failure)
        return
    if unconfirmed:
        status.defer_question(path)
    if not closed:
        print(f"review-links question {record.source.row}: " + " ".join(str(error).splitlines()), file=sys.stderr)


@dataclass(frozen=True)
class QuestionRun[Recipient, Result]:
    results: dict[Recipient, Result]
    error: Exception | None
    attempts: int


def run_question_delivery[Recipient, Result](
        recipients: tuple[Recipient, ...], authorize: Callable[[], None],
        send: Callable[[Recipient], Result], retry: Callable[[Result], bool]) -> QuestionRun[Recipient, Result]:
    """Check the saved question before each send attempt."""
    results: dict[Recipient, Result] = {}
    remaining = recipients
    attempts = 0
    try:
        settings = conf.read("review_links")
        tries = settings.whole("delivery_tries")
        pause = settings.number("delivery_pause_seconds")
        for _ in range(max(1, tries)):
            if attempts and pause:
                time.sleep(pause)
            attempts += 1
            for recipient in remaining:
                authorize()
                results[recipient] = send(recipient)
            remaining = tuple(recipient for recipient in remaining if retry(results[recipient]))
            if not remaining:
                break
    except Exception as error:
        return QuestionRun(results, error, attempts)
    return QuestionRun(results, None, attempts)


def deliver_page_question(delivery: PageQuestionSend, status: DeliveryStatus) -> DeliveryOutcome:
    """Publish the send marker before calling the transport."""
    job = delivery.job
    if isinstance(job, NoQuestionJob) or not job.start.acquire(blocking=False):
        return DeliveryUnconfirmed()
    path = status.path(delivery.page, delivery.row)
    try:
        sending = replace(job.record, revision=job.record.revision + 1, state=QuestionSendState.SENDING)
        expected = job.record
        verified = replace(job.delivery, record=sending)

        def authorize() -> None:
            nonlocal expected
            _authorize_question_send(status, path, expected, sending)
            expected = sending

        unconfirmed = sending.earlier_unconfirmed

        def send(_: staff.StaffId) -> QuestionAttempt:
            nonlocal unconfirmed
            result = _send_result(verified)
            unconfirmed |= result.state is QuestionSendState.UNKNOWN
            return result

        recipient = verified.saved.recipient
        run = run_question_delivery((recipient,), authorize, send,
                                    lambda result: result.state in (QuestionSendState.FAILED, QuestionSendState.UNKNOWN))
        result = run.results.get(recipient)
        if result is None:
            if run.error is not None and not isinstance(run.error, StaleQuestionAttempt):
                try:
                    with status.locked():
                        _finish_unstarted_question(status, path, run.error, job.record, sending)
                except (OSError, ValueError) as error:
                    _defer_question_error(status, path, delivery.row, error)
            return DeliveryUnconfirmed()
        retry_error = run.error
        failed = result.state is QuestionSendState.FAILED and not unconfirmed
        if isinstance(retry_error, ClosedQuestionSource) and result.state not in (
                QuestionSendState.SENT, QuestionSendState.QUEUED):
            result = replace(result, state=QuestionSendState.UNKNOWN, earlier_unconfirmed=False, error="")
        elif unconfirmed and result.state in (QuestionSendState.FAILED, QuestionSendState.UNKNOWN):
            result = replace(result, state=QuestionSendState.UNKNOWN, earlier_unconfirmed=True)
        try:
            with status.locked():
                advance_question_attempt(path, sending, result)
        except StaleQuestionAttempt:
            return DeliveryUnconfirmed()
        except (OSError, ValueError) as error:
            _defer_question_error(status, path, delivery.row, error)
            if failed:
                return DeliveryUnconfirmed()
            raise
        if result.state is QuestionSendState.UNKNOWN and result.earlier_unconfirmed:
            status.defer_question(path)
        if retry_error is not None and not isinstance(retry_error, (StaleQuestionAttempt, ClosedQuestionSource)):
            print(f"review-links question retry {delivery.row}: " + " ".join(str(retry_error).splitlines()), file=sys.stderr)
        elif failed and retry_error is None:
            try:
                with _lock_open_question_source(status, result):
                    pass
            except ClosedQuestionSource:
                return DeliveryUnconfirmed()
            except Exception as error:
                print(f"review-links question alert {delivery.row}: " + " ".join(str(error).splitlines()), file=sys.stderr)
                return DeliveryUnconfirmed()
            owner = verified.saved.alert_owner
            recipients: tuple[staff.StaffId, ...] = ()
            if owner is not None:
                if delivery.row.startswith("List item "):
                    recipients = tuple(runner.id for runner in staff_showrunner(owner, staff.snapshot()))
                else:
                    recipients = review_alert_showrunners(owner)
            print(f"review-links question {delivery.row}: " + " ".join(result.error.splitlines()), file=sys.stderr)
            if any(recipient != result.recipient_id for recipient in recipients):
                _ = alert_delivery_failures([DeliveryFailure(result.recipient_id, result.error)], run.attempts,
                                            source=delivery.page.review_list, row=delivery.row,
                                            message=delivery.message, recipients=recipients, attempt=delivery.attempt)
        if result.state in (QuestionSendState.SENT, QuestionSendState.QUEUED):
            return DeliveryConfirmed()
        if failed:
            return FailedDelivery(delivery.attempt, delivery.page.review_list, delivery.row, result.error)
        return DeliveryUnconfirmed()
    except StaleQuestionAttempt:
        return DeliveryUnconfirmed()
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
    if image["owner"] != record.recipient_id or image["id"] != source.item_id:
        raise ValueError("The Images question source has a different recipient or item ID.")
    return image


def recover_page_question(status: DeliveryStatus, path: Path, record: QuestionAttempt) -> None:
    """Check the source before recovery claims an unsent question."""
    if record.state is QuestionSendState.UNKNOWN:
        if not (record.earlier_unconfirmed or record.error):
            return
    elif record.state not in (QuestionSendState.PREPARED, QuestionSendState.READY,
                              QuestionSendState.CLAIMED, QuestionSendState.SENDING):
        return
    source = record.source
    source_path = Path(source.path)
    page = ReviewPage(source_path, source_path.parent, (source.row,), "")
    if status.path(page, source.row) != path:
        return
    with ExitStack() as locks:
        try:
            lock = _take_question_recovery_locks(locks, path, record)
        except OSError as error:
            _defer_question_error(status, path, source.row, error)
            return
        if lock is LockResult.BUSY:
            status.defer_question(path)
            return
        try:
            with status.locked():
                record = _repair_question_job_id(path, record)
            delivery = _claim_recovered_page_question(status, record, source_path, lock)
        except StaleQuestionAttempt:
            return
        except Exception as error:
            with status.locked():
                _finish_unstarted_question(status, path, error, record)
            return
        deliver(delivery, status)


def _decode_question_for_recovery(fields: dict[str, object]) -> QuestionAttempt:
    """Use the saved attempt ID when the job ID is damaged."""
    try:
        return delivery_state.decode_question_attempt(fields)
    except ValueError:
        data = fields.get("question_attempt")
        if not isinstance(data, dict):
            raise
        attempt = cast(dict[str, object], data)
        owner = attempt.get("owner")
        if not isinstance(owner, dict):
            raise
        return delivery_state.decode_question_attempt({"question_attempt": {
            **attempt, "owner": {**cast(dict[str, object], owner), "job_id": attempt.get("attempt_id")}}})


def _repair_question_job_id(path: Path, expected: QuestionAttempt) -> QuestionAttempt:
    """Repair the current record while recovery holds the attempt lock."""
    fields = delivery_state.read_fields(path)
    try:
        current = delivery_state.decode_question_attempt(fields)
    except ValueError:
        if _decode_question_for_recovery(fields) != expected:
            raise StaleQuestionAttempt("This question delivery job is stale.") from None
        unconfirmed = expected.earlier_unconfirmed or expected.state is QuestionSendState.SENDING
        repaired = replace(expected, revision=expected.revision + 1, owner=NoQuestionSendOwner(),
                           state=QuestionSendState.UNKNOWN if unconfirmed else QuestionSendState.READY,
                           earlier_unconfirmed=unconfirmed, resend_requests=())
        publish_question_attempt(path, repaired)
        return repaired
    if current != expected:
        raise StaleQuestionAttempt("This question delivery job is stale.")
    return current


def _claim_recovered_page_question(status: DeliveryStatus, record: QuestionAttempt,
                                   source_path: Path, lock: IO[str]) -> MessageDelivery | NoMessageDelivery:
    source = record.source
    if (source.row == f"{IMAGE_QUESTION_ROW_PREFIX}{source.item_id}/{source.question_id}"
            and source_path.name == image_store.RECORD):
        with status.locked():
            image = _read_image_question_source(status, record)
            questions = tuple(question_record.read(question) for question in image.get("questions", []))
            question = _verify_open_question(record, questions)
            saved = image_question_target(source_path, image).saved(question)
            return _recover_saved_question(status, saved, record, lock)
    elif source.row == f"List item {source.item_id.removeprefix('L')}/{source.question_id}":
        listing = _list_question_store(record)
        with listing.locked(), status.locked():
            fields, question = _open_list_question(listing, record)
            number = int(fields["number"])
            alert_owner = staff.StaffId(fields["owner"])
            if isinstance(source.keeper, NoSavedListKeeper):
                if source.row != f"List item {number}/{question.question_id}":
                    raise ValueError("The saved List question has a different row.")
                command = record.message.rpartition(_ANSWER_COMMAND_LINE)[2]
                saved = SavedQuestionDelivery(source, record.recipient_id, fields["title"], command, alert_owner)
            else:
                target = list_question_target(source_path, fields["id"], number, fields["title"],
                                              record.recipient_id, keeper=listing.owner, alert_owner=alert_owner)
                saved = target.saved(question)
            return _recover_saved_question(status, saved, record, lock)
    return NoMessageDelivery()


def _recover_saved_question(status: DeliveryStatus, saved: SavedQuestionDelivery,
                            record: QuestionAttempt, lock: IO[str]) -> PageQuestionSend:
    path = status.path(saved.page(), saved.source.row)
    return claim_question(status, ready_question(path, record, saved), lock=lock)


def stored_intent(intent: DeliveryIntent) -> StoredDeliveryIntent:
    return {"attempt": intent.attempt, "state": "pending", "recipients": list(intent.recipients)}


@dataclass(frozen=True)
class AttemptOwnership:
    """Own an attempt from its claim until its delivery ends."""

    row_file: Path
    state: Literal["claimed", "sending"]
    request_thread: threading.Thread = field(default_factory=threading.current_thread)


class DeliveryStatus:
    """Own delivery intents and serialize conditional attempt updates."""

    def __init__(self, root: Path) -> None:
        self.root: Path = root
        self.request_attempts: dict[str, AttemptOwnership] = {}
        self.question_jobs: WeakSet[IO[str]] = WeakSet()
        self.startup_recovery_pending: threading.Event = threading.Event()
        self.deferred_questions: dict[Path, MessageDelivery | None] = {}
        self.deferred_alerts: set[Path] = set()
        self.sending_alerts: set[Path] = set()
        self.deferred_delivery_lock: threading.Lock = threading.Lock()

    def has_deferred_questions(self) -> bool:
        with self.deferred_delivery_lock:
            return bool(self.deferred_questions)

    def defer_question(self, path: Path, delivery: MessageDelivery | None = None) -> None:
        with self.deferred_delivery_lock:
            self.deferred_questions[path] = delivery

    def take_deferred_questions(self) -> dict[Path, MessageDelivery | None]:
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
        self.root.mkdir(parents=True, exist_ok=True)
        with change_review.review_delivery_lock(self.root):
            yield

    def path(self, page: ReviewPage, row: str) -> Path:
        return change_routing.delivery_path(self.root, page.review_list, row)

    def previous_submission_source(self, request: str, current: Path) -> Path:
        """Find a delivery saved before moves retained their original paths."""
        for path in self.read_view().paths:
            try:
                fields = delivery_state.read_fields(path)
            except (OSError, ValueError):
                continue
            saved = fields.get("review_message")
            if not isinstance(saved, dict):
                continue
            record = cast(dict[str, object], saved)
            source = record.get("source")
            if record.get("submission_request") == request and isinstance(source, str):
                original = Path(source)
                if change_routing.delivery_path(self.root, original, request) == path:
                    return original
        return current

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

    def _image_question_message(self, page: ReviewPage, row: str) -> dict[str, object]:
        try:
            value = cast(object, json.loads(self.path(page, row).read_text(encoding="utf-8")))
        except (FileNotFoundError, ValueError):
            return {}
        if isinstance(value, dict):
            saved = cast(dict[str, object], value).get("review_message")
            if isinstance(saved, dict):
                return cast(dict[str, object], saved)
        return {}

    def image_question_dispatch(self, page: ReviewPage, row: str) -> ImageQuestionDispatch | NoImageQuestionDispatch:
        dispatch = self._image_question_message(page, row).get("image_question_dispatch")
        if isinstance(dispatch, dict):
            fields = cast(dict[str, object], dispatch)
            attempt, state = fields.get("attempt"), fields.get("state")
            if isinstance(attempt, str) and state in ("started", "started_after_unconfirmed", "accepted"):
                return {"attempt": attempt, "state": state}
        return NoImageQuestionDispatch()

    def closeout_path(self, page: ReviewPage, row: str) -> Path:
        name = self.path(page, row).name.removeprefix(DELIVERY_PREFIX)
        return self.root / (CLOSEOUT_PREFIX + name + ".json")

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
        """Read a failure unless an earlier send may have reached the owner."""
        failed = self.closeout_fields(page, row).get("failed_delivery")
        if failed is None:
            return NoFailedDelivery()
        dispatch = self.image_question_dispatch(page, row)
        if not isinstance(dispatch, NoImageQuestionDispatch) and (
                dispatch["state"] != "started" or dispatch["attempt"] != failed["attempt"]):
            return NoFailedDelivery()
        return FailedDelivery(failed["attempt"], Path(failed["review_list"]), failed["row"], failed["last_error"])

    def closeout_fields(self, page: ReviewPage, row: str) -> StoredDeliveryCloseout:
        try:
            return cast(StoredDeliveryCloseout, json.loads(self.closeout_path(page, row).read_text(encoding="utf-8")))
        except FileNotFoundError:
            return {"review_list": str(page.review_list.resolve()), "row": row, "messages": []}

    @staticmethod
    def write_closeout(path: Path, fields: StoredDeliveryCloseout) -> None:
        atomic_file.write_bytes_atomic(path, (json.dumps(fields) + "\n").encode("utf-8"), mode=0o600)

    def start_review_dispatch_locked(self, delivery: MessageDelivery, slots: DeliverySlots,
                                     intent: DeliveryIntent, *, question: bool = False) -> bool:
        """Save the attempt before sending. Finish an attempt that already has a result."""
        page, row = delivery.page, delivery.row
        fields = self.closeout_fields(page, row)
        if question:
            dispatch = fields.get("question_dispatch")
            has_receipt = intent.kind == "question" and self.has_review_save_receipt(page, row, intent)
            if has_receipt or (dispatch is not None and dispatch["attempt"] == intent.attempt
                               and (dispatch["state"] == "finished"
                                    or (intent.kind == "verdict" and dispatch["state"] == "failed"))):
                self.finish_intent_locked(delivery, slots, intent)
                return False
            if dispatch is not None and dispatch["attempt"] == intent.attempt and dispatch["state"] in ("started", "unknown"):
                # Retry with the saved message ID.
                return True
            started: StoredDeliveryCloseout = {**fields, "question_dispatch": {
                "attempt": intent.attempt, "state": "started"}}
            self.save_dispatch_start(page, row, fields, started)
            return True
        dispatch = fields.get("review_dispatch")
        if dispatch is not None and dispatch["attempt"] == intent.attempt:
            if dispatch["state"] == "started" and dispatch.get("keyed"):
                return True
            if dispatch["state"] != "finished":
                print(f"review-links delivery {row}: send result unknown for attempt {intent.attempt}; message kept",
                      file=sys.stderr)
                return False
            failed = fields.get("failed_delivery")
            if not (isinstance(page, ReviewMessagePage) and page.saved.get("submission_request")
                    and failed is not None and failed["attempt"] == intent.attempt):
                self.finish_intent_locked(delivery, slots, intent)
                return False
        review_started: StoredDeliveryCloseout = {**fields, "review_dispatch": {
            "attempt": intent.attempt, "state": "started", "keyed": True}}
        self.save_dispatch_start(page, row, fields, review_started)
        return True

    def save_dispatch_start(self, page: ReviewPage, row: str, previous: StoredDeliveryCloseout,
                            started: StoredDeliveryCloseout) -> None:
        path = self.closeout_path(page, row)
        try:
            self.write_closeout(path, started)
        except atomic_file.AtomicDurabilityUnconfirmed:
            # Restore the previous state. The sender has not run.
            self.write_closeout(path, previous)
            raise

    def record_result_locked(self, delivery: MessageDelivery, result: FailedDelivery | NoFailedDelivery,
                             alerts: tuple[PendingDeliveryAlert, ...] = (), *, question_relayed: bool = False,
                             question_id: str = "") -> None:
        """Keep alert history and the delivery result while the caller holds the delivery lock."""
        path = self.closeout_path(delivery.page, delivery.row)
        fields = self.closeout_fields(delivery.page, delivery.row)
        dispatch = fields.get("review_dispatch")
        if dispatch is not None and dispatch["attempt"] == delivery.attempt:
            dispatch["state"] = "finished"
        question = fields.get("question_dispatch")
        if question_id or (question is not None and question["attempt"] == delivery.attempt):
            state = "failed" if isinstance(result, FailedDelivery) and not question_relayed else "finished"
            updated: StoredQuestionDispatch = {"attempt": delivery.attempt, "state": state}
            if question_id:
                updated["question_id"] = question_id
            if isinstance(result, FailedDelivery):
                updated["error"] = result.last_error
            fields["question_dispatch"] = updated
        if alerts:
            fields["pending_alerts"] = [*fields.get("pending_alerts", []), *alerts]
        if isinstance(result, FailedDelivery):
            fields["failed_delivery"] = {"attempt": result.attempt, "review_list": str(result.review_list.resolve()),
                                         "row": result.row, "last_error": result.last_error}
        else:
            _ = fields.pop("failed_delivery", None)
            if not fields["messages"] and dispatch is None and question is None and not fields.get("pending_alerts"):
                path.unlink(missing_ok=True)
                return
        self.write_closeout(path, fields)

    def send_pending_alerts(self, path: Path) -> None:
        """Keep each alert until the sender confirms its result."""
        with self.deferred_delivery_lock:
            if path in self.sending_alerts:
                self.deferred_alerts.add(path)
                return
            self.sending_alerts.add(path)
            self.deferred_alerts.discard(path)
        retry = False
        try:
            saved = cast(StoredDeliveryCloseout, json.loads(path.read_text(encoding="utf-8")))
            for alert in saved.get("pending_alerts", []):
                try:
                    outcome = send_to_staff(alert["recipient"], SENDER, alert["message"], key=alert.get("key", ""))
                    if outcome == "pending":
                        retry = True
                        continue
                except SEND_ERRORS as error:
                    print(f"review-links delivery alert {path.name}: {error}", file=sys.stderr)
                    if not isinstance(error, FinalSendFailure):
                        retry = True
                        continue
                with self.locked():
                    fields = cast(StoredDeliveryCloseout, json.loads(path.read_text(encoding="utf-8")))
                    alerts = fields.get("pending_alerts", [])
                    if alert in alerts:
                        alerts.remove(alert)
                        self.write_closeout(path, fields)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError) as error:
            retry = retry or isinstance(error, OSError)
            print(f"review-links delivery alert {path.name}: {error}", file=sys.stderr)
        finally:
            with self.deferred_delivery_lock:
                self.sending_alerts.remove(path)
                if retry:
                    self.deferred_alerts.add(path)

    def notify_closeout_failure(self, page: ReviewPage, row: str, message: str,
                                recipients: tuple[staff.StaffId, ...]) -> None:
        """Send each row's failure once, including across server restarts."""
        if not recipients:
            return
        path = self.closeout_path(page, row)
        with self.locked():
            sent = self.closeout_fields(page, row)
            if message in sent["messages"]:
                return
            key = review_delivery_key(page.review_list, row, message, purpose="closeout")
            for identity in recipients:
                _ = send_to_staff(identity, SENDER, message, key=key)
            sent["messages"].append(message)
            self.write_closeout(path, sent)

    def slots(self, page: ReviewPage, row: str) -> DeliverySlots:
        return read_change_delivery(self.path(page, row))

    def validators(self, page: ReviewPage, row: str) -> tuple[staff.StaffId, ...]:
        try:
            fields = delivery_state.read_fields(self.path(page, row))
        except (FileNotFoundError, ValueError):
            return ()
        return validation_recipients(fields)

    def metadata_page(self, page: ReviewPage) -> ReviewPage:
        """Preserve the saved message when an answer changes its delivery slots."""
        try:
            fields = cast(StoredDeliverySlots, json.loads(self.path(page, page.rows[0]).read_text()))
        except FileNotFoundError:
            return page
        if 'user_answer' in fields:
            saved = fields['user_answer']
            return UserQuestionPage(page.review_list, Path(saved['production']), page.rows, page.token, saved)
        if 'review_message' in fields:
            return ReviewMessagePage(page.review_list, page.production, page.rows, page.token, fields['review_message'])
        return page

    def write_slots(self, page: ReviewPage, row: str, slots: DeliverySlots, *,
                    new_handoff: bool = False, answered_at: AnswerTime = NO_ANSWER_TIME) -> None:
        """Write both slots while the caller holds the delivery lock."""
        fields: StoredDeliverySlots = {}
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
            fields["question"] = {"question_id": question.question_id, "text": question.text, "delivery": (
                stored_intent(question.delivery)
                if isinstance(question.delivery, DeliveryIntent) else None)}
        elif isinstance(question, AnsweredQuestion):
            fields["question"] = {"question_id": question.question_id, "text": question.text, "answer": question.answer}
        saved_question = fields.get("question")
        if saved_question is not None and isinstance(question, (OpenFeedback, CompletedFeedback)):
            saved_question["kind"] = "feedback"
        path = self.path(page, row)
        if fields and re.fullmatch(r"C\d+", row):
            saved_times = {} if new_handoff else saved_delivery_times(path)
            if new_handoff:
                fields["handed_at"] = datetime.fromtimestamp(time.time(), user_zone()).isoformat()
            elif (saved := saved_times.get("handed_at")) is not None:
                fields["handed_at"] = saved
            if isinstance(question, AnsweredQuestion):
                if isinstance(answered_at, datetime):
                    fields["answered_at"] = answered_at.isoformat()
                elif (saved := saved_times.get("answered_at")) is not None:
                    fields["answered_at"] = saved
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
               *, alerts: tuple[PendingDeliveryAlert, ...] = (), question_relayed: bool = False) -> None:
        with self.locked():
            _ = self.request_attempts.pop(delivery.attempt, None)
            page, row = delivery.page, delivery.row
            slots = self.slots(page, row)
            if isinstance(page, ReviewMessagePage) and row.startswith(IMAGE_QUESTION_ROW_PREFIX):
                dispatch = self.image_question_dispatch(page, row)
                if (not isinstance(dispatch, NoImageQuestionDispatch) and dispatch["state"] != "accepted"
                        and dispatch["attempt"] == delivery.attempt):
                    if isinstance(result, NoFailedDelivery):
                        accepted = replace(page, saved={**page.saved, "image_question_dispatch": {
                            "attempt": delivery.attempt, "state": "accepted"}})
                        self.write_slots(accepted, row, slots)
                    self.record_result_locked(delivery, result)
                    return
            for current in slots.intents():
                if current.attempt == delivery.attempt:
                    tracked_question = is_change_question(delivery, slots, current)
                    self.record_result_locked(delivery, result, alerts, question_relayed=question_relayed)
                    if isinstance(result, FailedDelivery) and ((tracked_question and not question_relayed) or (
                            isinstance(page, ReviewMessagePage) and page.saved.get("submission_request"))):
                        return
                    self.finish_intent_locked(delivery, slots, current)
                    return

    def finish_intent_locked(self, delivery: MessageDelivery, slots: DeliverySlots, intent: DeliveryIntent) -> None:
        page, row = delivery.page, delivery.row
        if intent.kind == "validation":
            updated = replace(slots, validation=ValidationRequested(intent.attempt, intent.feedback))
        else:
            updated = slots.updated(intent.kind, NoDelivery())
        if intent.kind == "verdict" and isinstance(page, ReviewMessagePage) and page.saved.get("submission_request"):
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
            return self.ask_locked(page, row, slots, text, recipients, kind, question_id=secrets.token_hex(4))

    def ask_locked(self, page: ReviewPage, row: str, slots: DeliverySlots, text: str,
                   recipients: tuple[staff.StaffId, ...], kind: Literal["question", "feedback"], *,
                   question_id: str, during_validation: bool = False) -> QuestionDelivery:
        reason = delivery_state.require_waiting_why(text)
        intent = DeliveryIntent(secrets.token_hex(), "question", recipients=recipients)
        request = OpenFeedback if kind == "feedback" else OpenQuestion
        updated = replace(slots, question=request(question_id, text, intent))
        if not during_validation:
            updated = replace(updated, validation=NoDelivery(), waiting_why=reason)
        self.write_slots(page, row, updated, new_handoff=not during_validation)
        self.request_attempts[intent.attempt] = AttemptOwnership(self.path(page, row), "claimed")
        return QuestionDelivery(question_id, text, intent.attempt)

    def answer_question(self, page: ReviewPage, row: str, question_id: str, text: str) -> bool:
        """Save an answer once and preserve the delivery record's other slots."""
        return isinstance(self.save_question_answer(page, row, question_id, text), datetime)

    def save_question_answer(self, page: ReviewPage, row: str, question_id: str, text: str) -> AnswerTime:
        """Return the saved reply time when a new answer is written."""
        if not text.strip():
            raise question_record.AnswerRefused("enter an answer")
        text = text.strip()
        if isinstance(self.question_for_reply(self.slots(page, row), question_id, text), AnsweredQuestion):
            return NO_ANSWER_TIME
        console_text.require(text, "reply", identity=f"{page.review_list}/{row}/{question_id}",
                             refusal=question_record.AnswerRefused)
        with self.locked():
            slots = self.slots(page, row)
            question = self.question_for_reply(slots, question_id, text)
            if isinstance(question, AnsweredQuestion):
                return NO_ANSWER_TIME
            record = answer_row(page, row)
            if record.get("Verdict"):
                raise question_record.AnswerRefused("this row already has a saved answer")
            production = row_production(page, record)
            completed = AnsweredQuestion
            if isinstance(question, OpenFeedback):
                completed = CompletedFeedback
                events = review_events.decode(record.get("Review log", ""))
                if not any(event.matches_feedback(question.question_id, question.text) for event in events):
                    change_review.record_review_log(page.review_list, row, "feedback: " + question.text, production,
                                                    feedback_request_id=question.question_id)
            answered_at = datetime.now(user_zone())
            self.write_slots(page, row, replace(slots, question=completed(question_id, question.text, text)),
                             answered_at=answered_at)
            return answered_at

    @staticmethod
    def question_for_reply(slots: DeliverySlots, question_id: str, text: str) -> OpenQuestion | AnsweredQuestion:
        """Check the exchange ID and answer against the saved delivery slots."""
        try:
            return delivery_state.question_exchange_for_reply(slots.question, question_id, text)
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
</style><style>''' + page_frame.COPY_STYLE + '''</style></head><body><main>''' + content
        + "</main><script>" + page_frame.ZOOM_SCRIPT + page_frame.COPY_SCRIPT + "</script><script>"
        + page_frame.CONSOLE_READY_SCRIPT + "</script></body></html>").encode("utf-8")


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
    for verdict in page_frame.VerdictAction:
        buttons.append(page_frame.verdict_button(page_frame.VerdictItemType.CHANGE, verdict,
                                                selected=notice.outcome == verdict))
    for value, label in (("validate", "Ask showrunner to check"), ("question", "Ask a question"),
                         ("feedback", "Send feedback")):
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


def change_details(row: dict[str, str], key: str, *, validating: bool = False) -> str:
    """Show the change's facts as aligned lines, always open."""
    details = ChangeDetails.read(row, key).lines()
    if verdict := row.get("Verdict"):
        details.append(("verdict", verdict))
    elif validating:
        details.append(("state", "With the showrunner for validation"))
    return ('<dl class="change-details">'
            + "".join(f"<dt>{label}:</dt><dd>{html.escape(value)}</dd>" for label, value in details)
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
    return document(page_frame.action_failure("change-save-failure", "Not saved: " + notice.reason)
                    + answer_form(page, notice, feedback_rows))


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
    fields = status.closeout_fields(page, row)
    dispatch, failed = fields.get("question_dispatch"), fields.get("failed_delivery")
    if dispatch is None or dispatch["state"] in ("started", "unknown"):
        return False
    if "question_id" in dispatch:
        return dispatch["question_id"] == question.question_id and "error" in dispatch
    if failed is None or dispatch["attempt"] != failed["attempt"]:
        return False
    current = question.delivery
    if not isinstance(current, DeliveryIntent):
        current = status.slots(page, row).verdict
    if isinstance(current, DeliveryIntent):
        return current.attempt == failed["attempt"]
    # Older change records can discard the intent after a failure alert.
    return re.fullmatch(r"C\d+", row) is not None


def question_html(question: NoQuestion | OpenQuestion | AnsweredQuestion, *, failed: bool = False,
                  names: staff_names.StaffNames | None = None) -> str:
    if isinstance(question, NoQuestion):
        return ""
    feedback = isinstance(question, (OpenFeedback, CompletedFeedback))
    content = ('<h2>Feedback</h2>' if feedback else '<h2>Question</h2>') + review_html(question.text)
    if isinstance(question, AnsweredQuestion):
        if feedback:
            names = names if names is not None else staff_names.StaffNames(staff.StaffSnapshot(()))
            label = 'Showrunner:'
            answer = review_html(label + ' ' + question.answer)
            prefix = '<p>' + label
            if answer.startswith(prefix):
                answer = '<p>' + names.role_label('showrunner', label) + answer[len(prefix):]
            content += answer
        else:
            content += '<h2>Answer</h2>' + review_html(question.answer)
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


def list_routing(snapshots: dict[Path, change_review.ReviewListSnapshot]) -> change_routing.StaffRouting:
    documents = change_routing.production_documents(dict(showrunners.timer_docs()),
        ((listing.stem.removeprefix(change_review.PREFIX), snapshot.text) for listing, snapshot in snapshots.items()))
    return change_routing.StaffRouting(staff.snapshot(), documents)


def render(page: ReviewPage, delivery: DeliveryStatus, notice: AnswerNotice = NO_ANSWER_NOTICE) -> bytes:
    feedback_rows = conf.read("review_links").whole("feedback_rows")
    names = staff_names.StaffNames(staff.StaffSnapshot(()))
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
            warning = "Not saved: " + notice.reason
        alert = page_frame.action_failure("change-save-failure", warning) if warning else ""
        form_notice = notice if notice.row == row["#"] else AnswerNotice(row["#"])
        answer = (answer_form(page, form_notice, feedback_rows)
                  if slots.waits_for_user(bool(verdict)) and not change_on_list(row) else "")
        proof_title = "Try it · What you should see" if row[change_review.KIND] == "See in action" else "Proof"
        content = f'''{alert}
<h1>{html.escape(row["Change"])}</h1>
<h2>{proof_title}</h2>{review_html(row[change_review.CHECK])}
{question_html(question, failed=failed, names=names)}{answer}
{change_details(row, change_review.display_id(row, route_owner(page, row).tab), validating=slots.validation_requested)}'''
        sections.append(f'<section id="{row_id}" data-change-card>' + change_content(row, content) + '</section>')
    if notice.reason and not notice.row:
        sections.insert(0, page_frame.action_failure("change-save-failure", "Not saved: " + notice.reason))
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
def send_to_staff(identity: staff.StaffId, sender: str, message: str, *, key: str) -> Literal["skipped", "pending"] | None: ...


def send_to_staff(identity: staff.StaffId, sender: str, message: str, *, key: str = "") -> Literal["skipped", "pending"] | None:
    """Resolve the current conversation immediately before each send."""
    member = staff.snapshot().by_id(identity)
    if member is None:
        raise RuntimeError(f"no staff record for {identity}")
    address = f"session:{member.current_session}"
    if key:
        return send_message(address, sender, message, key=key, recipient_key=identity)
    _ = send_message(address, sender, message)
    return None


def review_send_options(key: str, recipient_key: str) -> tuple[str, ...]:
    """Use the saved attempt and staff ID for every copy of this send."""
    return ("--review-delivery", "--key", key, "--recipient-key", recipient_key,
            "--repeat-minutes", "52560000")


class UnconfirmedSend(RuntimeError):
    """The sender could not confirm whether the recipient accepted the message."""


class FinalSendFailure(RuntimeError):
    """The sender returned a terminal failure."""


def send_message(recipient: str, sender: str, message: str, *, key: str = "",
                 recipient_key: str = "") -> Literal["skipped", "pending"] | None:
    if not key:
        unrouted_alert.send_message(recipient, sender, message)
        return None
    outcome, error = unrouted_alert.message_outcome(recipient, sender, message, *review_send_options(key, recipient_key))
    if isinstance(outcome, send_result.UnconfirmedSendOutcome):
        raise UnconfirmedSend(error)
    result = outcome.header.outcome
    if result == "retry":
        raise UnconfirmedSend(error)
    if result in ("skipped", "pending"):
        return result
    if result in ("sent", "kept", "queued"):
        return None
    raise FinalSendFailure(error)


def feature_review_id(page: ReviewPage, doc: Path, row_id: str) -> str:
    """Find the feature-log proposal for a Change row."""
    lines = doc.read_text(encoding="utf-8").splitlines()
    try:
        paths = change_review.enrolled_review_checkouts(doc, lines)
    except Refusal as error:
        raise RuntimeError(str(error)) from error
    if isinstance(paths, change_review.EnrolledReviewCheckouts):
        repo = paths.main
    else:
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


def feedback_request_id(token: str, row: dict[str, str], words: str, slots: DeliverySlots) -> str:
    """Keep the ID when a Send retries before the delivery slot changes."""
    if isinstance(slots.question, AnsweredQuestion):
        previous = slots.question.question_id
    else:
        log = review_events.decode(row.get("Review log", ""))
        previous = next((event.time.isoformat() for event in reversed(log) if not event.feedback_request_id), "")
    request = json.dumps((token, row["#"], words.splitlines(), previous))
    return hashlib.sha256(request.encode()).hexdigest()[:32]


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
    filing = _change_work_filing.get()

    def file_work(current: dict[str, str]) -> investigation.ListDestination | investigation.NoListDestination:
        if outcome not in ("validate", "feedback", "not works"):
            return investigation.NoListDestination.ABSENT
        return filing(page, current, assignment, outcome, feedback)

    def mark_moved(destination: investigation.ListDestination | investigation.NoListDestination) -> None:
        if isinstance(destination, investigation.ListDestination):
            change_review.record_review_log(page.review_list, row_id, f"moved to List item {destination.number}",
                                            message_page.production)

    if outcome in CHANGE_HANDOFFS:
        with change_review.checked_review_write(page.review_list, status.root, lambda: None):
            current = answer_row(page, row_id)
            if change_on_list(current):
                raise ChangeHandoffRefused("Open the report on the List.")
            if current.get("Verdict"):
                raise ChangeHandoffRefused("this row already has a saved answer")
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
            if not during_validation and isinstance(owner, change_routing.UnroutedChange):
                raise question_record.AnswerRefused("this row has no showrunner to receive the request")
            sender = question_sender(row, recipients) if during_validation else None
            if during_validation and sender is None:
                raise question_record.AnswerRefused("the saved validator is unavailable")
            _ = row_production(page, row)
            destination = file_work(current)
            if outcome == "validate":
                _ = next_item.move_to_end(next_item.CHANGE, page.production, key=queue_key(page, row_id), prepared=prepared)
                change_review.record_review_log(page.review_list, row_id,
                    validation_request_words(feedback), message_page.production)
                attempt = status.begin_locked(page, row_id, slots, "validation", feedback, recipients=recipients,
                    waiting_why=feedback.strip() or "The user asked the showrunner to validate this change.")
                message = validation_message(message_page, row, attempt, feedback)
            else:
                request_feedback = outcome == "feedback"
                question_id = (feedback_request_id(token, current, fields["feedback"][0], slots)
                               if request_feedback else secrets.token_hex(4))
                if request_feedback:
                    change_review.record_review_log(page.review_list, row_id, "feedback: " + feedback,
                                                    message_page.production, feedback_request_id=question_id)
                intent = status.ask_locked(page, row_id, slots, feedback, recipients,
                                            "feedback" if request_feedback else "question",
                                            question_id=question_id, during_validation=during_validation)
                if not request_feedback:
                    try:
                        change_review.record_review_log(page.review_list, row_id, "question: " + intent.text,
                                                        message_page.production, feedback_request_id=intent.question_id)
                    except BaseException:
                        del status.request_attempts[intent.attempt]
                        raise
                attempt = intent.attempt
                message = _request_message(message_page, row, intent.question_id, intent.text, request_feedback,
                                           sender=sender)
            try:
                mark_moved(destination)
            except (OSError, ValueError, RuntimeError):
                _ = status.request_attempts.pop(attempt, None)
                raise
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
            _ = status.request_attempts.pop(attempt, None)

    if recipients and not defer_verdict_cleanup:
        attempt = status.begin(page, row_id, "verdict", recipients=recipients, user_verdict=True)
    try:
        if recipients and defer_verdict_cleanup:
            attempt = status.begin(page, row_id, "verdict", recipients=recipients, user_verdict=True)
        review = closeout_before_save(page, doc, row_id, verdict, status)
        production_name = unit_lookup.doc_owner(page.production, staff.snapshot()).name
        production = change_review.production_doc(production_name)
        with change_review.checked_review_write(page.review_list, status.root, lambda: None):
            current = answer_row(page, row_id)
            recorded = current.get('Verdict')
            if recorded == verdict:
                cancel_preparation()
                return NoMessageDelivery()
            if recorded:
                raise AnswerRefused("this row already has a different saved answer")
            if change_on_list(current):
                raise ChangeHandoffRefused("Open the report on the List.")
            cleanup = replace(change_review.PendingVerdictCleanup.for_row(status.root, page.review_list, row_id),
                              verdict=verdict, feature_review=review, require_git_commit=defer_verdict_cleanup or bool(review))
            destination = file_work(current)
            try:
                cleanup.save()
                result = change_review.save_verdict(page.review_list, row_id, verdict, production, cleanup=cleanup)
            except (change_review.ChangeReviewError, OSError):
                cleanup.intent.unlink(missing_ok=True)
                raise
            if isinstance(result, change_review.VerdictRefused) or isinstance(result.cleanup, change_review.NoVerdictCleanup):
                cleanup.intent.unlink(missing_ok=True)
            if not isinstance(result, change_review.VerdictRefused):
                try:
                    mark_moved(destination)
                except (OSError, ValueError, RuntimeError) as error:
                    print(f"review-links {row_id}: the List report is saved. Source log: {error}", file=sys.stderr)
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
                    recipients: tuple[staff.StaffId, ...]) -> tuple[str, str] | None:
    if not recipients:
        return None
    validator = staff.snapshot().by_id(recipients[0])
    if validator is None:
        return None
    return change_review.display_id(row, validator.name), validator.name


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


class UnconfirmedDelivery(DeliveryFailure):
    """The sender has no final result for this recipient."""


def review_delivery_key(source: Path, row: str, attempt: str, *, purpose: str = "message") -> str:
    """Identify one send across retries and server restarts."""
    identity = json.dumps((str(source.resolve()), row, attempt, purpose))
    return "review:" + hashlib.sha256(identity.encode()).hexdigest()


def send_with_retries(sender: str, recipients: tuple[staff.StaffId, ...], message: str, tries: int,
                      pause: float, *, key: str,
                      skipped: set[staff.StaffId] | None = None,
                      errors: tuple[type[Exception], ...] = SEND_ERRORS,
                      confirm: bool = False) -> list[DeliveryFailure]:
    """Retry only failed sends. The sender keeps queued and unconfirmed sends."""
    remaining = recipients
    failures: list[DeliveryFailure] = []
    for attempt in range(tries):
        if not remaining:
            break
        failures = []
        for target in remaining:
            try:
                result = send_to_staff(target, sender, message, key=key)
                if result == "pending" and confirm:
                    failures.append(UnconfirmedDelivery(target, "The sender has not confirmed delivery."))
                if result == "skipped" and skipped is not None:
                    skipped.add(target)
            except errors as error:
                failure = UnconfirmedDelivery if confirm and isinstance(error, UnconfirmedSend) else DeliveryFailure
                failures.append(failure(target, str(error)))
        remaining = tuple(failure.recipient for failure in failures if isinstance(failure.recipient, str))
        if remaining and attempt + 1 < tries:
            time.sleep(pause)
    return failures


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
    if question.delivery == intent:
        return question
    if intent.kind == "verdict":
        if isinstance(page, ReviewMessagePage) and not page.saved.get("submission_request"):
            return question
        if isinstance(page, UserQuestionPage) and page.saved.get("kind") == "question":
            return question
    return NoQuestion()


def check_review_question_source(page: ReviewPage, row: str, question: OpenQuestion) -> None:
    """Check the record that owns the open Review exchange."""
    path = page.review_list
    try:
        _ = path.stat()
        if isinstance(page, UserQuestionPage):
            records = update_registration.selected_question_records([path], (), None, owner=page.saved["owner"])
            current = next(iter(records), None)
            if current is not None and current.status == 'open':
                followup = current.item.get('followup')
                if followup is not None and followup['text'] == question.text:
                    if 'review_submission' not in current.item:
                        return
                    submission = new_item_dialog.read_submission(current.item['review_submission'])
                    if reply_tokens.matches_reference(submission.reply_token_sha256, question.question_id):
                        return
        elif isinstance(page, ReviewMessagePage):
            if path.parent.name in ('investigations', 'archive') and re.fullmatch(r"I\d+", row):
                item = investigation.read_review_record(path)
                if item.id == row and item.state == investigation.InvestigationState.OPEN:
                    if (not isinstance(item.submission, new_item_dialog.ReviewSubmission)
                            or reply_tokens.matches_reference(item.submission.reply_token_sha256, question.question_id)):
                        return
            elif path.name.endswith('.review.json'):
                reviews = delivery_state.read_fields(path)
                if row in reviews:
                    from scripts.review_links.efficiency_reviews import decode
                    if decode(reviews[row]).mark == review_events.ReviewMark.WAITING:
                        return
            else:
                issue = Issue(path, record_store.read_header(path, strict=True))
                if proposal_issue.pending_review_reply(issue):
                    submission = proposal_issue.review_submission(issue)
                    if (not isinstance(submission, new_item_dialog.ReviewSubmission)
                            or reply_tokens.matches_reference(submission.reply_token_sha256, question.question_id)):
                        return
        elif any(record["#"] == row and not record.get("Verdict") for record in change_review.review_rows(path)):
            return
    except FileNotFoundError:
        pass
    raise ClosedQuestionSource("The Review question is no longer open.")


@dataclass(frozen=True)
class OutboundReviewMessage:
    text: str = field(repr=False)
    token: str = field(default="", repr=False)

    def redact(self, error: str) -> str:
        return error.replace(self.token, "[reply token]") if self.token else error


def scoped_reply_message(key: str, proof: reply_tokens.ReplyTokenProof, prefix: str = "") -> OutboundReviewMessage:
    """Build the reply command from a native proof."""
    token = reply_tokens.derive(proof)
    return OutboundReviewMessage(prefix + reply_command(key, token), token)


class ReportAwaitingList(OSError):
    """Keep the first notice until its current report and List row are available."""


def require_report_list_item(record: investigation.Investigation
                             ) -> investigation.ListDestination | investigation.NoListDestination:
    """Require the List row for a native report."""
    destination = record.list_destination
    if not record.belongs_on_list:
        return destination
    if not isinstance(destination, investigation.ListDestination):
        raise ReportAwaitingList("The report still needs a List item.")
    try:
        matches = investigation.list_destination_has_submission(destination, record.submission_scope)
    except (OSError, ValueError) as error:
        raise ReportAwaitingList("The List item is unavailable.") from error
    if not matches:
        raise ReportAwaitingList("The List item does not match the report.")
    return destination


def list_report_delivery_prefix(record: investigation.Investigation, prefix: str) -> str:
    """Open the report evidence on the List."""
    destination = require_report_list_item(record)
    if not isinstance(destination, investigation.ListDestination):
        return prefix
    text, separator, evidence = prefix.rpartition("\n\nEvidence in Review\n")
    if not separator:
        return prefix
    try:
        route = urlsplit(evidence)
    except ValueError:
        return prefix
    item_key = parse_qs(route.fragment).get("item", [])
    if (route.path != f"/{CONSOLE_FOLDER}/review.html" or not route.netloc or len(item_key) != 1
            or re.fullmatch(r"investigation/[^/]+/" + re.escape(record.id), item_key[0]) is None):
        return prefix
    url = route._replace(path=page_frame.LIST_PAGE_PATH,
                         fragment=page_frame.list_item_fragment(destination.keeper, destination.number)[1:])
    return text + "\n\nEvidence on the List\n" + url.geturl()

def outbound_review_message(delivery: MessageDelivery) -> OutboundReviewMessage:
    """Derive the scoped reply token from its current native note."""
    from scripts.review_links import review_submissions

    page = delivery.page
    scoped = page.saved.get("scoped_reply") if isinstance(page, (ReviewMessagePage, UserQuestionPage)) else None
    if scoped is None:
        return OutboundReviewMessage(delivery.message)
    prefix, separator, command = delivery.message.rpartition("\n")
    if command != reply_command(scoped["key"]):
        raise ValueError("The saved Review reply command does not match its key.")
    key = scoped["key"]
    if (isinstance(page, ReviewMessagePage) and page.saved.get("submission_request")
            and key.startswith(("submission/bug/", "submission/investigation/"))):
        try:
            with review_submissions.scoped_submission(new_item_dialog.read_submission_key(key)) as current:
                proof = current.details.reply_proof
                if isinstance(current.record, investigation.Investigation):
                    prefix = list_report_delivery_prefix(current.record, prefix)
        except review_submissions.SavedSubmissionMissing as error:
            raise ReportAwaitingList("The current report is unavailable.") from error
    else:
        proof = review_submissions.current_reply_proof(key)
    if proof.sha256 != scoped["token_sha256"]:
        raise reply_tokens.UnavailableReplyToken("The note has a different reply token.")
    return scoped_reply_message(scoped["key"], proof, prefix + separator)


def deliver_review_question(delivery: MessageDelivery, status: DeliveryStatus, intent: DeliveryIntent,
                            question: OpenQuestion) -> DeliveryOutcome:
    """Use the same send loop for a question stored in Review delivery slots."""
    page, row = delivery.page, delivery.row
    started = False
    finished = False
    outbound = OutboundReviewMessage(delivery.message)
    key = review_delivery_key(page.review_list, row, intent.attempt)

    def authorize() -> None:
        nonlocal started, finished, outbound
        with status.locked():
            slots = status.slots(page, row)
            if intent not in slots.intents() or review_open_question(page, slots, intent) != question:
                raise StaleQuestionAttempt("The Review question has changed.")
            check_review_question_source(page, row, question)
            metadata = status.metadata_page(page)
            if isinstance(metadata, (ReviewMessagePage, UserQuestionPage)):
                if metadata.saved["message"] != delivery.message:
                    raise ValueError("The saved Review message does not match this delivery.")
                if not metadata.saved["saved"] and not status.has_review_save_receipt(page, row, intent):
                    raise ValueError("The Review question save is unconfirmed.")
            if not started:
                if not status.start_review_dispatch_locked(delivery, slots, intent, question=True):
                    finished = True
                    raise StaleQuestionAttempt("The Review question has a delivery result.")
                started = True
            outbound = outbound_review_message(delivery)

    def send(recipient: staff.StaffId | MissingRecipient) -> DeliveryFailure | QuestionSendState:
        if isinstance(recipient, MissingRecipient):
            return DeliveryFailure(recipient, "no showrunner to send to")
        try:
            outcome = send_to_staff(recipient, SENDER, outbound.text, key=key)
        except UnconfirmedSend:
            return QuestionSendState.UNKNOWN
        except Exception as error:
            return DeliveryFailure(recipient, outbound.redact(str(error)))
        return QuestionSendState.UNKNOWN if outcome == "pending" else QuestionSendState.SENT

    try:
        recipients: tuple[staff.StaffId | MissingRecipient, ...] = delivery.recipients or (MissingRecipient(),)
        run = run_question_delivery(recipients, authorize, send,
                                   lambda result: result is QuestionSendState.UNKNOWN or (
                                       isinstance(result, DeliveryFailure) and isinstance(result.recipient, str)))
        error = run.error
        if isinstance(error, StaleQuestionAttempt):
            if finished and intent.kind == "verdict":
                status.send_pending_alerts(status.closeout_path(page, row))
            return DeliveryUnconfirmed()
        failures = [result for result in run.results.values() if isinstance(result, DeliveryFailure)]
        if isinstance(error, ClosedQuestionSource):
            with status.locked():
                slots = status.slots(page, row)
                if intent in slots.intents() and review_open_question(page, slots, intent) == question:
                    status.finish_intent_locked(delivery, slots, intent)
            return DeliveryUnconfirmed()
        if isinstance(error, reply_tokens.UnavailableReplyToken):
            _defer_question_error(status, status.path(page, row), row, error, delivery=delivery)
            return DeliveryUnconfirmed()
        if QuestionSendState.UNKNOWN in run.results.values():
            status.defer_question(status.path(page, row), delivery)
            if error is not None:
                print(f"review-links question {row}: " + " ".join(str(error).splitlines()), file=sys.stderr)
            return DeliveryUnconfirmed()
        reason = " ".join(str(error).splitlines()) if error is not None else ""
        if error is not None and not failures:
            failures = [DeliveryFailure(MissingRecipient(), reason)]
        result = (FailedDelivery(delivery.attempt, page.review_list, row, failures[-1].error)
                  if failures else NoFailedDelivery())
        relayed = False
        alerts: tuple[PendingDeliveryAlert, ...] = ()
        if failures and error is None:
            alert_recipients = delivery.alert_recipients
            if isinstance(page, (ReviewMessagePage, UserQuestionPage)):
                try:
                    alert_recipients = review_alert_showrunners(staff.StaffId(page.saved["owner"]))
                except (OSError, ValueError) as failure:
                    print(f"review-links delivery alert {row}: {failure}", file=sys.stderr)
                    alert_recipients = ()
            if intent.kind == "verdict":
                alerts = pending_delivery_alerts(delivery, failures, run.attempts, alert_recipients)
            if not alerts:
                relayed = alert_delivery_failures(failures, run.attempts, source=page.review_list, row=row,
                                                 message=delivery.message, recipients=alert_recipients,
                                                 attempt=delivery.attempt, errors=(Exception,))
        with status.locked():
            slots = status.slots(page, row)
            if intent not in slots.intents() or review_open_question(page, slots, intent) != question:
                return DeliveryUnconfirmed()
            if intent.kind == "question" and (isinstance(result, NoFailedDelivery) or relayed):
                status.write_save_receipt(page, row, intent)
            status.record_result_locked(delivery, result, alerts, question_relayed=relayed,
                                        question_id=question.question_id)
            if (isinstance(result, NoFailedDelivery) or relayed or error is not None
                    or isinstance(page, (ReviewMessagePage, UserQuestionPage))):
                status.finish_intent_locked(delivery, slots, intent)
        if alerts:
            status.send_pending_alerts(status.closeout_path(page, row))
        if error is not None:
            print(f"review-links question {row}: {reason}", file=sys.stderr)
        return result if isinstance(result, FailedDelivery) else DeliveryConfirmed()
    except (OSError, ValueError) as failure:
        _defer_question_error(status, status.path(page, row), row, failure, delivery=delivery)
        return DeliveryUnconfirmed()
    finally:
        status.release_recovery_claims((delivery.attempt,))


def deliver(delivery: MessageDelivery | NoMessageDelivery, status: DeliveryStatus) -> None:
    """Deliver each saved message and report exhausted attempts to its showrunner."""
    _ = deliver_with_outcome(delivery, status)


def deliver_with_outcome(delivery: MessageDelivery | NoMessageDelivery, status: DeliveryStatus) -> DeliveryOutcome:
    """Send the message and return the result of this attempt."""
    if isinstance(delivery, NoMessageDelivery):
        return DeliveryUnconfirmed()
    if isinstance(delivery, PageQuestionSend):
        return deliver_page_question(delivery, status)
    tracked_review = False
    page = delivery.page
    confirm = isinstance(page, ReviewMessagePage) and bool(page.saved.get("submission_request"))
    tries, pause = 0, 0.0
    with status.locked():
        slots = status.slots(delivery.page, delivery.row)
        ownership = status.request_attempts.get(delivery.attempt)
        if ownership is not None and ownership.state == "sending":
            return DeliveryUnconfirmed()
        intent = next((intent for intent in slots.intents() if intent.attempt == delivery.attempt), None)
        if intent is None:
            _ = status.request_attempts.pop(delivery.attempt, None)
            return DeliveryUnconfirmed()
        question = review_open_question(page, slots, intent)
        if isinstance(question, NoQuestion):
            settings = conf.read("review_links")
            tries = settings.whole("delivery_tries")
            pause = settings.number("delivery_pause_seconds")
            if isinstance(page, ReviewMessagePage) and delivery.row.startswith(IMAGE_QUESTION_ROW_PREFIX):
                previous = status.image_question_dispatch(page, delivery.row)
                unconfirmed = (not isinstance(previous, NoImageQuestionDispatch) and previous["state"] != "accepted"
                               and isinstance(status.failed_delivery(page, delivery.row), NoFailedDelivery))
                started = replace(page, saved={**page.saved, "image_question_dispatch": {
                    "attempt": delivery.attempt, "state": "started_after_unconfirmed" if unconfirmed else "started"}})
                status.write_slots(started, delivery.row, slots.updated("verdict", NoDelivery()))
            else:
                tracked_review = isinstance(page, (ReviewMessagePage, UserQuestionPage)) and intent.kind == "verdict"
                if tracked_review:
                    _ = status.request_attempts.pop(delivery.attempt, None)
                    if not status.start_review_dispatch_locked(delivery, slots, intent):
                        return DeliveryUnconfirmed()
        status.request_attempts[delivery.attempt] = AttemptOwnership(status.path(page, delivery.row), "sending")
    if isinstance(question, OpenQuestion):
        return deliver_review_question(delivery, status, intent, question)
    try:
        outbound = outbound_review_message(delivery)
    except ReportAwaitingList as error:
        status.release_recovery_claims((delivery.attempt,))
        print(f"review-links delivery {delivery.row}: {error}", file=sys.stderr)
        return DeliveryUnconfirmed()
    except (OSError, ValueError) as error:
        reason = " ".join(str(error).splitlines())
        failures = [DeliveryFailure(recipient, reason) for recipient in delivery.recipients]
        if not failures:
            failures = [DeliveryFailure(MissingRecipient(), reason)]
    else:
        key = review_delivery_key(page.review_list, delivery.row, delivery.attempt)
        failures = (send_with_retries(SENDER, delivery.recipients, outbound.text, tries, pause,
                                       key=key, confirm=confirm) if delivery.recipients
                    else [DeliveryFailure(MissingRecipient(), "no showrunner to send to")])
        failures = [replace(failure, error=outbound.redact(failure.error)) for failure in failures]
    result = (FailedDelivery(delivery.attempt, delivery.page.review_list, delivery.row, failures[-1].error)
              if failures else NoFailedDelivery())
    showrunners_for_alert = delivery.alert_recipients
    if failures and isinstance(page, (ReviewMessagePage, UserQuestionPage)):
        try:
            showrunners_for_alert = review_alert_showrunners(staff.StaffId(page.saved["owner"]))
        except (OSError, ValueError) as error:
            print(f"review-links delivery alert {delivery.row}: {error}", file=sys.stderr)
            showrunners_for_alert = ()
    routes = [(failure, tuple(recipient for recipient in showrunners_for_alert if recipient != failure.recipient))
              for failure in failures]
    alerts = pending_delivery_alerts(delivery, failures, tries, showrunners_for_alert) if tracked_review else ()
    try:
        status.record(delivery, result, alerts=alerts)
    except (OSError, ValueError) as error:
        if not confirm or not isinstance(result, FailedDelivery):
            raise
        print(f"review-links delivery result {delivery.row}: {error}", file=sys.stderr)
        return result
    if alerts:
        status.send_pending_alerts(status.closeout_path(delivery.page, delivery.row))
    for failure, recipients in routes:
        if not tracked_review or not recipients:
            _ = alert_delivery_failures([failure], tries, source=delivery.page.review_list, row=delivery.row,
                                        message=delivery.message, recipients=recipients, attempt=delivery.attempt)
    return result if isinstance(result, FailedDelivery) else DeliveryConfirmed()


def pending_delivery_alerts(delivery: MessageDelivery, failures: list[DeliveryFailure], tries: int,
                            recipients: tuple[staff.StaffId, ...]) -> tuple[PendingDeliveryAlert, ...]:
    source, row = delivery.page.review_list, delivery.row
    return tuple(
        PendingDeliveryAlert(recipient=recipient,
            key=review_delivery_key(source, row, delivery.attempt, purpose=f"alert:{failure.recipient}"),
            message=delivery_failure_alert(failure, tries, source, row, delivery.message))
        for failure in failures for recipient in recipients if recipient != failure.recipient)


def delivery_failure_alert(failure: DeliveryFailure, tries: int, source: Path, row: str, message: str,
                           title: str = "Change review delivery needs attention") -> str:
    target = "no recipient" if isinstance(failure.recipient, MissingRecipient) else failure.recipient
    return "\n".join((title, f"List: {source}", f"Row: {row}", f"Recipient: {target}", f"Tries: {tries}",
                      f"Last error: {failure.error}", "The user's answer is saved. Pass it on by hand.", message))


def alert_delivery_failures(failures: list[DeliveryFailure], tries: int, *, source: Path, row: str,
                            message: str, recipients: tuple[staff.StaffId, ...],
                            attempt: str, title: str = "Change review delivery needs attention",
                            errors: tuple[type[Exception], ...] = SEND_ERRORS) -> bool:
    reached = False
    for failure in failures:
        alert = delivery_failure_alert(failure, tries, source, row, message, title)
        targets = tuple(recipient for recipient in recipients if recipient != failure.recipient)
        if not targets:
            print(f"review-links delivery {row}: {failure.error}; no separate showrunner for the alert", file=sys.stderr)
        key = review_delivery_key(source, row, attempt, purpose=f"alert:{failure.recipient}")
        for runner in targets:
            try:
                _ = send_to_staff(runner, SENDER, alert, key=key)
                reached = True
            except errors as failure:
                print(f"review-links delivery alert {row}: " + " ".join(str(failure).splitlines()), file=sys.stderr)
                return reached
    return reached


def recover_verdict_cleanups(status: DeliveryStatus, intents: tuple[Path, ...],
                             unreadable: set[Path]) -> set[Path]:
    """Finish each durable verdict and return the deliveries that must wait."""
    unready: set[Path] = set()
    for intent in intents:
        if intent.is_symlink():
            continue
        try:
            with status.locked():
                cleanup = change_review.PendingVerdictCleanup.load(intent)
                listing = cleanup.path
                if listing in unreadable:
                    continue
                path = change_routing.delivery_path(status.root, listing, cleanup.row)
                with change_review.checked_review_write(listing, status.root, lambda: None):
                    if any(ownership.row_file == path for ownership in status.request_attempts.values()):
                        continue
                    try:
                        snapshot = change_review.review_snapshot(listing)
                    except (OSError, ValueError, change_review.ChangeReviewError) as error:
                        unreadable.add(listing)
                        print(f"review-links recovery {listing}: {error}", file=sys.stderr)
                        continue
                    saved = any(row['#'] == cleanup.row and row['Verdict'] == cleanup.verdict for row in snapshot.reviewed)
                    if not saved:
                        intent.unlink(missing_ok=True)
                        continue
                    try:
                        cleanup.confirm_durability()
                    except change_review.VerdictDurabilityUnconfirmed as error:
                        unready.add(path)
                        print(f"review-links recovery {intent.name}: {error}", file=sys.stderr)
                        continue
            for line in finish_saved_verdict(cleanup, status):
                print(f"review-links {line}", file=sys.stderr)
        except FileNotFoundError:
            continue
        except (OSError, ValueError, KeyError, TypeError, AttributeError, change_review.ChangeReviewError) as error:
            print(f"review-links recovery {intent.name}: {error}", file=sys.stderr)
    return unready


def recover_pending(status: DeliveryStatus, *,
                    only: change_review.PendingVerdictCleanup | change_review.NoVerdictCleanup = change_review.NO_VERDICT_CLEANUP) -> None:
    """Recover scratch cleanup before the saved deliveries."""
    targeted = isinstance(only, change_review.PendingVerdictCleanup)
    if not targeted:
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
        paths = status.read_view().paths
        listings = change_review.lists_directory().resolve().glob(change_review.PREFIX+"*.md")
    if not paths:
        return
    snapshots: dict[Path, change_review.ReviewListSnapshot] = {}
    for listing in listings:
        if listing in unreadable:
            continue
        try:
            snapshots[listing] = change_review.review_snapshot(listing)
        except (OSError, ValueError, change_review.ChangeReviewError) as error:
            print(f"review-links recovery {listing}: {error}", file=sys.stderr)
    routing: change_routing.StaffRouting | None = None
    for listing, snapshot in snapshots.items():
        for row in (*snapshot.pending, *snapshot.reviewed):
            if targeted and row["#"] != only.row:
                continue
            path = change_routing.delivery_path(status.root, listing, row["#"])
            if path not in paths or path in unready or path.is_symlink():
                continue
            recovered_attempts: tuple[str, ...] = ()
            try:
                with status.locked():
                    if any(ownership.row_file == path for ownership in status.request_attempts.values()):
                        continue
                    fields = cast(object, json.loads(path.read_text(encoding="utf-8")))
                    decoded = decode_change_delivery(fields)
                    intents = decoded.intents()
                    if not intents or any(intent.attempt in status.request_attempts for intent in intents):
                        continue
                    if isinstance(decoded.verdict, DeliveryIntent) and not row.get("Verdict"):
                        row_id = row["#"]
                        current = change_review.review_snapshot(listing)
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
                    if not recipients and (intent.kind == "validation" or validation_question):
                        raise ValueError("the saved validator is unavailable")
                    recipients = recipients or (verdict_recipients(owner, row["Verdict"])
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
                    deliver(MessageDelivery(page, row["#"], recipients, message, intent.attempt, alerts), status)
            except (OSError, ValueError, KeyError, TypeError, StopIteration) as error:
                print(f"review-links recovery {path.name}: {error}", file=sys.stderr)
            finally:
                status.release_recovery_claims(recovered_attempts)


def recover_deferred_questions(status: DeliveryStatus) -> None:
    """Retry the saved attempts after the current sender releases its claim."""
    for path, delivery in status.take_deferred_questions().items():
        try:
            if delivery is None:
                record = _decode_question_for_recovery(delivery_state.read_fields(path))
                recover_page_question(status, path, record)
            else:
                deliver(delivery, status)
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as error:
            _defer_question_error(status, path, path.name, error, delivery=delivery)


def recover_review_messages(status: DeliveryStatus) -> None:
    """Restore Review messages through the existing claims, retries and alerts."""
    for path in status.read_view().paths:
        claimed: tuple[str,...] = ()
        try:
            fields = delivery_state.read_fields(path)
            if "question_attempt" in fields:
                record = _decode_question_for_recovery(fields)
                recover_page_question(status, path, record)
                continue
            with status.locked():
                if any(owner.row_file == path for owner in status.request_attempts.values()):
                    continue
                fields = cast(StoredDeliverySlots,json.loads(path.read_text()))
                saved = fields.get('review_message')
                if saved is None:
                    continue
                if saved.get('submission_delivered'):
                    continue
                source = Path(saved['source'])
                row = saved.get('row',source.stem)
                if row.startswith((IMAGE_QUESTION_ROW_PREFIX, "List item ")):
                    continue
                page = ReviewMessagePage(source,source.parent,(row,),'',saved)
                slots = status.slots(page,row)
                if not saved['saved']:
                    if row.startswith(IMAGE_QUESTION_ROW_PREFIX):
                        status.clear_locked(page,row,slots,'verdict')
                        continue
                    if not isinstance(slots.verdict, DeliveryIntent):
                        continue
                    if not status.has_review_save_receipt(page, row, slots.verdict):
                        print(f'review-links recovery {path.name}: review save state unknown; message kept',
                              file=sys.stderr)
                        continue
                    page = replace(page, saved={**saved, 'saved': True})
                claimed = tuple(intent.attempt for intent in slots.intents())
                for attempt in claimed:
                    status.request_attempts[attempt] = AttemptOwnership(path,'claimed')
            for intent in slots.intents():
                deliver(MessageDelivery(page,row,intent.recipients,saved['message'],intent.attempt,
                                        (staff.StaffId(saved['owner']),)),status)
        except (OSError,ValueError,KeyError,TypeError) as error:
            print(f'review-links recovery {path.name}: {error}',file=sys.stderr)
        finally:
            status.release_recovery_claims(claimed)


def recover_question_answers(status: DeliveryStatus) -> None:
    """Recover saved user answers through the existing delivery claims and retry budget."""
    for path in status.read_view().paths:
        claimed: tuple[str, ...] = ()
        try:
            with status.locked():
                if any(owner.row_file == path for owner in status.request_attempts.values()):
                    continue
                fields = cast(StoredDeliverySlots, json.loads(path.read_text()))
                saved = fields.get("user_answer")
                if saved is None:
                    continue
                slots = delivery_state.decode_value(fields)
                intents = slots.intents()
                if not intents:
                    continue
                page = UserQuestionPage(Path(saved["source"]), Path(saved["production"]),
                                        (Path(saved["source"]).stem,), "", saved)
                try:
                    questions = update_registration.selected_question_records(
                        [page.review_list], (), None, owner=saved["owner"], strict=True)
                except OSError as error:
                    # The strict reader wraps errors from the source file.
                    if not isinstance(error.__cause__, FileNotFoundError):
                        raise
                    questions = []
                question = next(iter(questions), None)
                # An answer closes its question; a follow-up or a skip leaves it open.
                if question is None or question.status != ("closed" if saved.get("kind", "answer") in ("answer", "feedback") else "open"):
                    status.clear_locked(page, page.rows[0], slots, "verdict")
                    continue
                if not saved.get("saved", False):
                    if not status.has_review_save_receipt(page, page.rows[0], slots.verdict):
                        status.clear_locked(page, page.rows[0], slots, "verdict")
                        print(f"review-links recovery {path.name}: answer save state unknown; not sent", file=sys.stderr)
                        continue
                    page = replace(page, saved={**saved, "saved": True})
                claimed = tuple(intent.attempt for intent in intents)
                for attempt in claimed:
                    status.request_attempts[attempt] = AttemptOwnership(path, "claimed")
            for intent in intents:
                deliver(MessageDelivery(page, page.rows[0], intent.recipients, saved["message"], intent.attempt,
                                        (staff.StaffId(saved["owner"]),)), status)
        except (OSError, ValueError, KeyError, TypeError, update_registration.RegistrationFailure,
                update_registration.InputError) as error:
            print(f"review-links recovery {path.name}: {error}", file=sys.stderr)
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


def resend_submission_reply(reference: str) -> str:
    """Send the reply command to the current owner of a waiting item."""
    from scripts.review_links import review_items, review_submissions

    key = reference
    if not reference.startswith(new_item_dialog.KEY_PREFIX):
        kinds = {review_items.ReviewType.BUG: new_item_dialog.SubmissionKind.BUG,
                 review_items.ReviewType.QUESTION: new_item_dialog.SubmissionKind.QUESTION,
                 review_items.ReviewType.RESPONSE: new_item_dialog.SubmissionKind.QUESTION,
                 review_items.ReviewType.INVESTIGATION: new_item_dialog.SubmissionKind.INVESTIGATION,
                 review_items.ReviewType.PROPOSAL: new_item_dialog.SubmissionKind.PROPOSAL}
        matches: list[str] = []
        for item in review_items.read(review_link.review_root(), kinds=tuple(kinds)):
            identity = item.id.rsplit("/", 1)[-1]
            if reference not in (identity, item.id, item.key):
                continue
            details = review_items.submission_origin(item)
            if isinstance(details, new_item_dialog.ReviewSubmission):
                matches.append(new_item_dialog.submission_key(kinds[item.type], item.owner, identity, details.request_id))
        if not matches:
            raise question_record.AnswerRefused("No sent item has that ID.")
        if len(matches) > 1:
            raise question_record.AnswerRefused("More than one sent item has that ID. Use its full key.")
        key, = matches
    scoped = new_item_dialog.read_submission_key(key)
    with review_submissions.scoped_submission(scoped) as located:
        saved = review_submissions.renew_legacy_reply(located)
        if not saved.awaiting_reply:
            raise question_record.AnswerRefused("The item is not waiting for a reply.")
        if isinstance(saved.record, investigation.Investigation):
            _ = require_report_list_item(saved.record)
        command = scoped_reply_message(saved.key, saved.details.reply_proof)
        try:
            send_to_staff(saved.owner, SENDER, f"Reply command for {saved.id}.\n\n{command.text}")
        except SEND_ERRORS as error:
            raise question_record.AnswerRefused(command.redact(str(error))) from None
    return scoped.item_id


def answer_item(key: str, words: str, question_id: str, status: DeliveryStatus) -> bool:
    from scripts.review_links import decision_store, efficiency_reviews, review_items, review_submissions
    if key.startswith(review_submissions.KEY_PREFIX):
        try:
            return review_submissions.answer(key, words, question_id)
        except review_submissions.SubmissionError as error:
            raise question_record.AnswerRefused(str(error)) from error
    from scripts.production import investigation
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
            path = investigation.folder(source.record.owner)/(source.record.id+'.md')
            row = source.record.id
        else:
            path, row = efficiency_reviews.path(source.path), decision_store.efficiency_identity(item)
        page = ReviewPage(path, path.parent, (row,), '')
        text = words.strip()
        slots = status.slots(page, row)
        selected = slots.question
        tracked = not isinstance(selected, NoQuestion) or bool(question_id)
        if tracked and isinstance(status.question_for_reply(slots, question_id, text), AnsweredQuestion):
            return False

        save: Callable[[], bool | investigation.Investigation | None]
        if isinstance(source, review_items.QuestionSource):
            save = update_registration.prepare_reply_record(
                source.record, words, who, identities=update_registration.QuestionStaff.read(), question_id=question_id)
        elif isinstance(source, review_items.InvestigationSource):
            save = investigation.prepare_answer(
                source.record.owner, source.record.id, words, question_id=question_id)
        else:
            save = decision_store.prepare_answer(item, words, who)
            console_text.require(words, "reply", identity=f"{path}/{row}/{question_id}",
                                 refusal=question_record.AnswerRefused)

        with status.locked():
            slots = status.slots(page, row)
            question = slots.question
            if tracked and isinstance(status.question_for_reply(slots, question_id, text), AnsweredQuestion):
                return False
            if question != selected:
                raise question_record.AnswerRefused("The current question changed.")
            page = status.metadata_page(page)
            try:
                changed = save() is not False
            except decision_store.NoOpenQuestion as error:
                raise question_record.AnswerRefused(str(error)) from error
            if isinstance(question, OpenQuestion):
                status.write_slots(page, row, replace(slots, question=AnsweredQuestion(question.question_id,
                                                                                       question.text, text)))
            return changed
    if isinstance(source,review_items.ChangeSource):
        assert isinstance(source.assignment.production,Path)
        page = ReviewPage(source.path,source.assignment.production,(source.row['#'],),'')
        answered_at = status.save_question_answer(page, source.row['#'], question_id, words)
        if not isinstance(answered_at, datetime):
            return False
        change_review.record_review_log(source.path, source.row['#'], 'Answer: ' + words, page.production, who,
                                        feedback_request_id=answer_log_id(question_id), event_time=answered_at)
        return True
    raise question_record.AnswerRefused('this item cannot receive an answer')


def _forward_reply(machine: str, key: str, words: str, token: str) -> int:
    from scripts.notify.review import review_timeout_seconds
    arguments = shlex.join(["answer-question", "--item=" + key, "--question=" + token, "--text=" + words])
    command = '"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/review_links/change_page.py" ' + arguments
    result = subprocess.run(["ssh", "-o", "BatchMode=yes", machine, command], stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=30 + review_timeout_seconds(), check=False)
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

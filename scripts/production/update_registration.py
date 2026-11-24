#!/usr/bin/env python3
"""Register updates, questions and answers for a production.

Question records use the proposals folder or the owner's question folder.
Fixed staff ids identify their askers.
The JSON list stores ordinary reminders only.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from importlib import import_module
from pathlib import Path
from collections.abc import Callable, Generator, Iterable, Iterator, Sequence
from contextlib import AbstractContextManager, ExitStack, contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Literal, NamedTuple, NotRequired, TypedDict, cast, overload
from zoneinfo import ZoneInfo

from scripts.production.production_doc import (
    EnrolledPaths, Production, Refusal, read_production, resolve_production_paths,
)
from scripts.production.production_admission import require_admission
from scripts.production.checkpoint_gate import report

from scripts.production import change_routing, delivery_state, showrunners, session_staff, showrunner_tree, staff, unit_lookup
from scripts.production.proposals_folder import issues_folder
from scripts.production.store_owner import showrunner_id
from scripts.production import record_store
from scripts.production.record_store import has_line_break, state_root
from scripts.production.issues import Issue, frontmatter
from scripts.lib import urgency
from scripts.lib.urgency import Urgency, UrgencyError
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.git_exclude import require_untracked_ignored
from scripts.lib.user_zone import user_zone

from scripts.review_links import new_item_dialog, question_path, reply_tokens, review_events
from scripts.usage import agents_log

from scripts.lib import conf, names
from scripts.lib.process_failure import failure_detail
from scripts.agents.remote import local_machine


class InputError(Exception):
    pass


STARTED = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")
UNREADABLE_LIST = "needs-user list unreadable"
PLUMBING = re.compile(r"\b(?:berth|reservations?|incursions?|holders?|file holds?|file clash\w*|writers?|testers?|retak\w*|re-?shoot\w*)\b", re.IGNORECASE)

@dataclass(frozen=True)
class DueNow:
    pass


@dataclass(frozen=True)
class DeferredUntil:
    at: datetime


@dataclass(frozen=True)
class AwaitingReply:
    """The user sent the asker a follow-up; the question is not due until the asker replies."""
    sent: datetime
    later: datetime | question_path.NoLaterTime = question_path.NO_LATER


OutstandingTiming = DueNow | DeferredUntil | AwaitingReply


def outstanding_later(timing: OutstandingTiming) -> datetime | question_path.NoLaterTime:
    if isinstance(timing, DeferredUntil):
        return timing.at
    if isinstance(timing, AwaitingReply):
        return timing.later
    return question_path.NO_LATER


@dataclass(frozen=True)
class Followup:
    """A follow-up the user sent the asker, and the asker's reply."""
    followup: str
    reply: str


@dataclass(frozen=True)
class Ranking:
    """The item's urgency and the preceding item's text."""
    urgency: Urgency = Urgency()
    follows: str = ""


@dataclass(frozen=True)
class OutstandingReminder:
    since: datetime
    text: str
    timing: OutstandingTiming = DueNow()
    rank: Ranking = Ranking()


@dataclass(frozen=True)
class OutstandingQuestion:
    since: datetime
    text: str
    question: str
    timing: OutstandingTiming = DueNow()
    rank: Ranking = Ranking()
    # The fixed staff id identifies the asker for the answer relay.
    asked_by: str = ""
    followups: tuple[Followup, ...] = ()


@dataclass(frozen=True, kw_only=True)
class SentOutstandingQuestion(OutstandingQuestion):
    key: str
    recipient: staff.StaffId

    @property
    def reference_text(self) -> str:
        return f"{self.text} ({self.key})"

    def read_command(self) -> str:
        return ("~/.claude/scripts/lib/py ~/.claude/scripts/production/update_registration.py answer "
                f"--production {shlex.quote(self.recipient)} --key {shlex.quote(self.key)}")


@dataclass(frozen=True)
class ReviewedOutstandingQuestion:
    since: datetime
    text: str
    question: str
    reviewed: str
    timing: OutstandingTiming = DueNow()
    rank: Ranking = Ranking()
    # The fixed staff id identifies the asker for the answer relay.
    asked_by: str = ""
    followups: tuple[Followup, ...] = ()


@dataclass(frozen=True)
class ReviewUnavailableOutstandingQuestion:
    since: datetime
    text: str
    question: str
    reason: str
    timing: OutstandingTiming = DueNow()
    rank: Ranking = Ranking()
    # The fixed staff id identifies the asker for the answer relay.
    asked_by: str = ""
    followups: tuple[Followup, ...] = ()


Outstanding = (OutstandingReminder | OutstandingQuestion | ReviewedOutstandingQuestion
               | ReviewUnavailableOutstandingQuestion)


def check_keys(fields: dict[str, object], allowed: set[str], where: str) -> None:
    unknown = sorted(set(fields) - allowed)
    if unknown:
        raise InputError(f"{where}: unknown field(s) {', '.join(unknown)}; allowed: {', '.join(sorted(allowed))}")


def read_parts(value: object, where: str) -> QuestionParts:
    """Read stored question parts; the full question text stays in `question` for every other reader."""
    if not isinstance(value, dict):
        raise InputError(f"{where}.parts: expected an object")
    fields = cast(dict[str, object], value)
    check_keys(fields, {"ask", "context", "items", "recommend", "choices"}, where + ".parts")
    texts = {key: fields.get(key) for key in ("ask", "context", "recommend")}
    lists = {key: fields.get(key) for key in ("items", "choices")}
    if not all(isinstance(text, str) for text in texts.values()) or not all(
            isinstance(entries, list) and all(isinstance(entry, str) for entry in cast(list[object], entries))
            for entries in lists.values()):
        raise InputError(f"{where}.parts: expected text ask, context and recommend, and lists of text items and choices")
    return QuestionParts(ask=cast(str, texts["ask"]), context=cast(str, texts["context"]),
                         items=cast(list[str], lists["items"]), recommend=cast(str, texts["recommend"]),
                         choices=cast(list[str], lists["choices"]))


def question_text(parts: QuestionParts) -> str:
    """Write the parts as one full question for /next, messages, the review and older readers."""
    blocks = [parts["ask"], parts["context"],
              "\n".join(f"{number}. {item}" for number, item in enumerate(parts["items"], 1)),
              f"Recommended: {parts['recommend']}" if parts["recommend"] else "",
              "Choices:\n" + "\n".join(f"- {choice}" for choice in parts["choices"]) if parts["choices"] else ""]
    return "\n\n".join(block for block in blocks if block.strip())


@dataclass(frozen=True)
class ValidQuestionTitle:
    text: str


@dataclass(frozen=True)
class RefusedQuestionTitle:
    reason: str


def check_question_title(text: str) -> ValidQuestionTitle | RefusedQuestionTitle:
    """Check the title before a new or changed question reaches storage."""
    limit = conf.read_or_repository("production").whole("question_title_max_chars")
    if not text.strip() or has_line_break(text):
        return RefusedQuestionTitle("must be one nonempty line; put the rest in --context and --item")
    if len(text) > limit:
        return RefusedQuestionTitle(f"has {len(text)} characters; the limit is {limit}; put the rest in --context and --item")
    return ValidQuestionTitle(text)


def plumbing(line: str) -> str | None:
    """Why the line fails the plumbing check, or None when it passes."""
    found = PLUMBING.search(line)
    if found is None:
        return None
    return (f"{found.group(0)!r} is the production's own plumbing or shorthand, which the user never sees; "
            + "say what changes in the app, or leave it out")


def read_ranking(fields: dict[str, object], where: str) -> Ranking:
    """An item's `urgency`, `due`, `marked` and `follows`; none of them is a normal item."""
    follows = fields.get("follows", "")
    if not isinstance(follows, str) or "\n" in follows:
        raise InputError(f"{where}.follows: expected one line of text")
    try:
        return Ranking(urgency.from_fields(fields), follows.strip())
    except UrgencyError as error:
        raise InputError(f"{where}: {error}") from None


def read_pending_followup(value: object, where: str) -> PendingFollowup | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise InputError(f"{where}.followup: expected an object")
    fields = cast(dict[str, object], value)
    check_keys(fields, {"text", "sent"}, where + ".followup")
    text, sent = fields.get("text"), fields.get("sent")
    if not isinstance(text, str) or not text.strip():
        raise InputError(f"{where}.followup.text: expected the follow-up text")
    if not isinstance(sent, str) or not STARTED.match(sent):
        raise InputError(f"{where}.followup.sent: expected YYYY-MM-DDTHH:MM")
    return PendingFollowup(text=text, sent=sent)


def read_followups(value: object, where: str) -> tuple[Followup, ...]:
    if not isinstance(value, list):
        raise InputError(f"{where}.followups: expected a list")
    exchanges: list[Followup] = []
    for index, entry in enumerate(cast(list[object], value)):
        if not isinstance(entry, dict):
            raise InputError(f"{where}.followups[{index}]: expected an object")
        fields = cast(dict[str, object], entry)
        check_keys(fields, {"followup", "reply", "question_id", "replied_at"}, f"{where}.followups[{index}]")
        followup, reply = fields.get("followup"), fields.get("reply")
        if not isinstance(followup, str) or not isinstance(reply, str) or not followup.strip() or not reply.strip():
            raise InputError(f"{where}.followups[{index}]: expected the follow-up and its reply")
        if "question_id" in fields and (not isinstance(fields["question_id"], str) or not fields["question_id"]):
            raise InputError(f"{where}.followups[{index}].question_id: expected an ID")
        if "replied_at" in fields:
            try:
                stamp = fields["replied_at"]
                if not isinstance(stamp, str):
                    raise ValueError
                _ = datetime.fromisoformat(stamp)
            except ValueError:
                raise InputError(f"{where}.followups[{index}].replied_at: expected a time") from None
        exchanges.append(Followup(followup, reply))
    return tuple(exchanges)


def check_reply_log_positions(value: object, exchanges: int, where: str) -> None:
    if not isinstance(value, dict):
        raise InputError(f"{where}.reply_log_positions: expected an object")
    positions = cast(dict[object, object], value)
    exchange_numbers = {str(number) for number in range(exchanges)}
    if not all(number in exchange_numbers and type(index) is int and index >= 0
               for number, index in positions.items()):
        raise InputError(f"{where}.reply_log_positions: expected exchange numbers and log positions")


def parse_outstanding_entries(entries: object, path: Path) -> list[Outstanding]:
    """Validate reminder and question fields from their owning stores."""
    if not isinstance(entries, list):
        raise InputError(f"{path}: expected a JSON list")
    items: list[Outstanding] = []
    for index, entry in enumerate(cast(list[object], entries)):
        where = f"{path}[{index}]"
        if not isinstance(entry, dict):
            raise InputError(f"{where}: expected an object")
        fields = cast(dict[str, object], entry)
        check_keys(fields, {"since", "text", "after", "question", "parts", "reviewed", "review_unavailable", "urgency",
                            "due", "marked", "follows", "asked_by", "followup", "followups", "review_id", "review_log", "waiting_why",
                            "review_submission", "reply_log_positions", "answered_at"}, where)
        details: new_item_dialog.SubmissionOrigin = new_item_dialog.NoReviewSubmission()
        if "review_submission" in fields:
            try:
                details = new_item_dialog.read_submission(fields["review_submission"])
            except ValueError as error:
                raise InputError(f"{where}.review_submission: {error}") from error
        if "parts" in fields:
            _ = read_parts(fields["parts"], where)
        rank = read_ranking(fields, where)
        since, text, after = fields.get("since"), fields.get("text"), fields.get("after")
        if not isinstance(since, str) or not STARTED.match(since):
            raise InputError(f"{where}.since: expected YYYY-MM-DDTHH:MM")
        try:
            later = question_path.read_legacy_later(after)
        except ValueError as error:
            raise InputError(f"{where}.after: {error}") from error
        if not isinstance(text, str) or not text.strip():
            raise InputError(f"{where}.text: expected nonempty text")
        # Read legacy titles so the replacement can name their problems.
        timing: OutstandingTiming = DueNow() if isinstance(later, question_path.NoLaterTime) else DeferredUntil(later)
        pending = read_pending_followup(fields.get("followup"), where)
        if pending is not None:
            timing = AwaitingReply(datetime.fromisoformat(pending["sent"]), later)
        followups = read_followups(fields.get("followups", []), where)
        if "reply_log_positions" in fields:
            check_reply_log_positions(fields["reply_log_positions"], len(followups), where)
        if "answered_at" in fields:
            answered_at = fields["answered_at"]
            try:
                if not isinstance(answered_at, str):
                    raise ValueError
                _ = datetime.fromisoformat(answered_at)
            except ValueError:
                raise InputError(f"{where}.answered_at: expected a time") from None
        since_at = datetime.fromisoformat(since)
        question = fields.get("question")
        reviewed, unavailable = fields.get("reviewed"), fields.get("review_unavailable")
        asked_by = fields.get("asked_by", "")
        if not isinstance(asked_by, str) or "\n" in asked_by or ("asked_by" in fields and not asked_by.strip()):
            raise InputError(f"{where}.asked_by: expected one non-empty line")
        if "question" not in fields:
            if any(key in fields for key in ("reviewed", "review_unavailable", "asked_by", "followup", "followups")):
                raise InputError(f"{where}: question required for a review result, an asker or a follow-up")
            items.append(OutstandingReminder(since_at, text, timing, rank))
            continue
        if not isinstance(question, str) or not question.strip():
            raise InputError(f"{where}.question: expected non-empty full question")
        if "reviewed" in fields:
            if not isinstance(reviewed, str) or not reviewed.strip() or "review_unavailable" in fields:
                raise InputError(f"{where}.reviewed: expected completed review without an unavailable result")
            items.append(ReviewedOutstandingQuestion(since_at, text, question, reviewed, timing, rank, asked_by, followups))
        elif "review_unavailable" in fields:
            if not isinstance(unavailable, str):
                raise InputError(f"{where}.review_unavailable: expected reason text")
            items.append(ReviewUnavailableOutstandingQuestion(since_at, text, question, unavailable, timing, rank, asked_by,
                                                              followups))
        elif isinstance(details, new_item_dialog.ReviewSubmission):
            identity = fields.get("review_id")
            if not isinstance(identity, str) or not re.fullmatch(r"Q[1-9][0-9]*", identity) or not asked_by:
                raise InputError(f"{where}: a sent Question requires a native ID and recipient.")
            recipient = staff.StaffId(asked_by)
            key = new_item_dialog.submission_key(new_item_dialog.SubmissionKind.QUESTION, recipient, identity,
                                                 details.request_id)
            items.append(SentOutstandingQuestion(since_at, text, question, timing, rank, asked_by, followups,
                                                 key=key, recipient=recipient))
        else:
            items.append(OutstandingQuestion(since_at, text, question, timing, rank, asked_by, followups))
    return sorted(items, key=lambda item: item.since)


class ReportItem(TypedDict):
    """A report that the user requested until the specified time."""
    text: str
    until: str


def reports_path(name: str, *, identities: QuestionStaff | None = None) -> Path:
    path = outstanding_file(name, identities=identities)
    return path.parent.parent / "reports" / path.name


class InstancePresent(NamedTuple):
    output: str


class InstanceAbsent(NamedTuple):
    pass


class QuestionParts(TypedDict):
    """The parts of a question that the Questions page shows as separate sections."""
    ask: str
    context: str
    items: list[str]
    recommend: str
    choices: list[str]


class PendingFollowup(TypedDict):
    """A follow-up that waits for the asker's reply."""
    text: str
    sent: str


class FollowupExchange(TypedDict):
    followup: str
    reply: str
    question_id: NotRequired[str]
    replied_at: NotRequired[str]


class OutstandingItem(TypedDict):
    waiting_why: NotRequired[str]
    review_id: NotRequired[str]
    review_log: NotRequired[str]
    review_submission: NotRequired[dict[str, object]]
    since: str
    text: str
    after: NotRequired[str]
    question: NotRequired[str]
    parts: NotRequired[QuestionParts]
    reviewed: NotRequired[str]
    review_unavailable: NotRequired[str]
    urgency: NotRequired[str]
    due: NotRequired[str]
    marked: NotRequired[str]
    follows: NotRequired[str]
    asked_by: NotRequired[str]
    followup: NotRequired[PendingFollowup]
    followups: NotRequired[list[FollowupExchange]]
    reply_log_positions: NotRequired[dict[str, int]]
    answered_at: NotRequired[str]


class QuestionRegistration(TypedDict):
    time: str
    text: str
    reviewed: NotRequired[str]
    question: NotRequired[str]
    withdrawn: NotRequired[str]


class AnsweredItem(TypedDict):
    time: str
    name: str
    text: str
    question: NotRequired[str]
    asked_by: NotRequired[str]


@dataclass(frozen=True)
class UnitAsker:
    unit: str


@dataclass(frozen=True)
class ShowrunnerAsker:
    pass


@dataclass(frozen=True)
class AskerNotRecorded:
    reason: str


@dataclass(frozen=True)
class OpenItemWithoutAsker:
    pass


@dataclass(frozen=True)
class StaffAsker:
    member_id: staff.StaffId


@dataclass(frozen=True)
class ProducerQuestions:
    """A producer owns questions without a production document."""

    owner: staff.StaffRow

    @property
    def name(self) -> str:
        return self.owner.name

    @property
    def zone(self) -> ZoneInfo:
        return user_zone()


QuestionScope = Production | ProducerQuestions


def question_owner(reference: str, record: staff.StaffSnapshot) -> staff.StaffRow | None:
    """Resolve a fixed staff reference or a producer's current name."""
    return record.by_member_reference(reference) or next(
        (row for row in record.rows if row.kind == "producer" and row.name == reference), None)


def question_scope(reference: str | staff.StaffRow, record: staff.StaffSnapshot) -> QuestionScope:
    """Read the question owner without requiring a producer to have a production."""
    owner = reference if isinstance(reference, staff.StaffRow) else question_owner(reference, record)
    return ProducerQuestions(owner) if owner is not None and owner.kind == "producer" else read_production(reference, record)


def question_source(scope: QuestionScope) -> Path | staff.StaffRow:
    """Select a production document or a producer's fixed staff row."""
    return scope.owner if isinstance(scope, ProducerQuestions) else scope.doc


@dataclass(frozen=True)
class QuestionStaff:
    """One command's staff identities and current display labels."""

    record: staff.StaffSnapshot
    sources: session_staff.MemberSources

    @classmethod
    def read(cls, *, sources: list[Path] | None = None) -> QuestionStaff:
        if sources is None:
            return cls.for_record(staff.snapshot())
        record = staff.snapshot()
        return cls(record, session_staff.member_sources(dependencies=sources.append))

    @classmethod
    def for_record(cls, record: staff.StaffSnapshot,
                   sources: session_staff.MemberSources | None = None) -> QuestionStaff:
        if sources is None:
            dependencies = staff.RecordInputs()
            sources = session_staff.member_sources(dependencies=dependencies)
        return cls(record, sources)

    def owner(self, document: Path) -> staff.StaffRow:
        """Select the production owner from the command's staff snapshot."""
        try:
            return unit_lookup.doc_owner(document, self.record)
        except staff.StaffUnreadable as error:
            raise RegistrationFailure("questions", str(error)) from error

    def asker(self, production: QuestionScope, unit: str = "") -> staff.StaffRow:
        parent = production.owner
        if not unit:
            return parent
        row = self.record.by_member_reference(unit)
        if row is not None:
            if row.kind != "unit" or row.parent != staff.ReportsTo(parent.id):
                raise RegistrationFailure("ask", f"{unit} is not a unit of {production.name}")
            return row
        selector = session_staff.UnitMember(production.name, unit)
        found = session_staff.unit_match(selector, self.record, parent=session_staff.RequiredParent(parent))
        if isinstance(found, session_staff.NoSuchMember):
            raise RegistrationFailure("ask", f"{unit} is not a unit of {production.name}")
        return found.row

    def display(self, row: staff.StaffRow) -> tuple[str, str]:
        parent = row if row.kind in ("showrunner", "producer") else self.record.parent_of(row)
        return parent.name if parent else "", row.name

    def documents(self) -> dict[str, staff.StaffRow]:
        """Return local showrunner rows by name."""
        return {row.name: row for row in self.record.rows
                if row.kind == "showrunner" and isinstance(row.doc, (staff.LiveDoc, staff.WrappedDoc))
                and row.doc.machine == local_machine()}

    def production_of(self, session_id: str) -> str | None:
        """Return the current question owner's staff name."""
        row = self.record.by_current_session(session_id) if session_id else None
        return row.name if row is not None and row.kind in ("showrunner", "producer") else None

    def names(self, name: str, below: bool = False) -> list[str]:
        """Select descendants through fixed parent ids, including owners without timers."""
        owner = question_owner(name, self.record)
        if owner is not None and owner.kind == "producer":
            return [owner.name]
        return showrunner_tree.user_facing(name, below, record=self.record)


QuestionAsker = StaffAsker | UnitAsker | ShowrunnerAsker | AskerNotRecorded | OpenItemWithoutAsker


def question_asker(fields: OutstandingItem | AnsweredItem,
                   missing: OpenItemWithoutAsker | AskerNotRecorded,
                   identities: QuestionStaff | None = None) -> QuestionAsker:
    name = fields.get("asked_by")
    if not isinstance(name, str) or not name.strip():
        return missing
    if identities is not None:
        row = identities.record.by_member_reference(name)
        if row is not None:
            return StaffAsker(row.id)
        if name != SHOWRUNNER_ASKER:
            return AskerNotRecorded(f"staff member {name} is unavailable")
    return ShowrunnerAsker() if name == SHOWRUNNER_ASKER else UnitAsker(name)


@dataclass(frozen=True)
class NoAnswerRelay:
    """The owning showrunner receives the page answer."""


@dataclass(frozen=True)
class AnswerRelay:
    """The CLI line and fixed recipient describe the same relay decision."""

    line: str
    recipient: staff.StaffId | NoAnswerRelay
    unit: str = ""


def answer_relay(production: QuestionScope, name: str, asker: QuestionAsker,
                 identities: QuestionStaff | None = None) -> AnswerRelay:
    identities = identities if identities is not None else QuestionStaff.read()

    def target(line: str, reference: str = "", unit: str = "") -> AnswerRelay:
        row = identities.record.by_member_reference(reference) if reference else None
        if row is None and reference:
            row = next((member for member in identities.record.rows
                        if member.kind == "showrunner" and member.name == reference), None)
        return AnswerRelay(line, row.id if row is not None else NoAnswerRelay(), unit)

    owner = "this production" if name == production.name else f"{name}, below this production"
    if isinstance(asker, OpenItemWithoutAsker):
        return target(f"relay: none; the item belongs to {owner}")
    if isinstance(asker, AskerNotRecorded):
        return target(f"relay: none; {asker.reason}; the item belongs to {owner}")
    if isinstance(asker, StaffAsker):
        row = identities.record.by_member_reference(asker.member_id)
        if row is None:
            return target(f"relay: none; staff member {asker.member_id} is unavailable")
        if row.kind == "producer":
            return target(f"relay: to {row.name} (staff {row.id})", row.id)
        current_owner = production.owner
        current_name, label = identities.display(row)
        if row.kind == "showrunner":
            if row.id == current_owner.id:
                return target("relay: none; this showrunner asked it")
            loaded = names.runtime_names()
            label = loaded.job("showrunner") if isinstance(loaded, names.Names) else "requesting role (display name unavailable)"
        elif row.parent == staff.ReportsTo(current_owner.id):
            current_name = production.name
        if current_name != production.name:
            return target(f"relay: to the showrunner of {current_name}, for {label} (staff {row.id})", current_name, label)
        return target(f"relay: to {label} (staff {row.id})", row.id, label)
    if name != production.name:
        if isinstance(asker, UnitAsker):
            label = asker.unit
        else:
            loaded = names.runtime_names()
            label = loaded.job("showrunner") if isinstance(loaded, names.Names) else "requesting role (display name unavailable)"
        return target(f"relay: to the showrunner of {name}, for {label}", name, label)
    if isinstance(asker, ShowrunnerAsker):
        return target("relay: none; this showrunner asked it")
    return target(f"relay: to {asker.unit}", asker.unit, asker.unit)



def relay_line(production: QuestionScope, name: str, asker: QuestionAsker,
               identities: QuestionStaff | None = None) -> str:
    return answer_relay(production, name, asker, identities).line


def answered_items(outstanding: Path, now: datetime) -> list[AnsweredItem]:
    """Read the retained answers, converting JSON fields before replaying them."""
    history = outstanding.with_suffix(".answered.jsonl")
    if not history.exists():
        return []
    retained: list[AnsweredItem] = []
    for line in history.read_text(encoding="utf-8", newline="\n").split("\n"):
        try:
            value = cast(object, json.loads(line))
            if not isinstance(value, dict):
                continue
            fields = cast(dict[str, object], value)
            stamp, name, text = fields.get("time"), fields.get("name"), fields.get("text")
            if not isinstance(stamp, str) or not isinstance(name, str) or not isinstance(text, str):
                continue
            dated = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if dated.tzinfo is None or dated < now - timedelta(hours=48):
                continue
            entry = AnsweredItem(time=stamp, name=name, text=text)
            question = fields.get("question")
            if isinstance(question, str):
                entry["question"] = question
            asker = fields.get("asked_by")
            if isinstance(asker, str) and asker.strip():
                entry["asked_by"] = asker
            retained.append(entry)
        except ValueError:
            continue
    return retained


def record_answer(outstanding: Path, name: str, item: OutstandingItem, now: datetime) -> None:
    retained = [entry for entry in answered_items(outstanding, now) if entry["text"] != item["text"]]
    entry = AnsweredItem(time=now.isoformat(), name=name, text=item["text"])
    if "question" in item:
        entry["question"] = item["question"]
    if "asked_by" in item:
        entry["asked_by"] = item["asked_by"]
    write_text_atomic(outstanding.with_suffix(".answered.jsonl"),
                      "".join(json.dumps(saved) + "\n" for saved in [*retained, entry]))


def replay_answer(production: QuestionScope, item: AnsweredItem, identities: QuestionStaff | None = None) -> None:
    dated = datetime.fromisoformat(item["time"].replace("Z", "+00:00"))
    print(f"already answered {dated:%a %b %d %H:%M}")
    print(relay_line(production, item["name"],
                     question_asker(item, AskerNotRecorded("asked_by not recorded in answered record"), identities), identities))
    report("answer", "ok", f"{item['name']}: {item['text']}")


class UnitJudgment(NamedTuple):
    unit: str
    phase: str
    wait: str


class OpenReminder(NamedTuple):
    text: str
    after: str
    rank: Ranking = Ranking()


class QuestionForReview(NamedTuple):
    text: str
    after: str
    question: str
    rank: Ranking = Ranking()


class OpenQuestionWithoutText(NamedTuple):
    text: str
    after: str
    rank: Ranking = Ranking()


OpenForUserItem = OpenReminder | QuestionForReview | OpenQuestionWithoutText


class KeepOutstanding(NamedTuple):
    pass


class ReplaceOutstanding(NamedTuple):
    items: tuple[OpenForUserItem, ...]


class KeepReports(NamedTuple):
    pass


class ReplaceReports(NamedTuple):
    items: tuple[ReportItem, ...]


class ShowrunnerState(NamedTuple):
    units: tuple[UnitJudgment, ...]
    merges_held: tuple[str, ...]
    outstanding: KeepOutstanding | ReplaceOutstanding
    reports: KeepReports | ReplaceReports = KeepReports()
    # What the showrunner owes and what sets each off; a resume acts on it first.
    owed: tuple[str, ...] = ()


class RegistrationFailure(Exception):
    def __init__(self, step: str, detail: str) -> None:
        super().__init__(detail)
        self.step: str = step


def command(step: str, arguments: list[str], *, cwd: Path | None = None) -> str:
    result = subprocess.run(arguments, cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RegistrationFailure(step, failure_detail(result))
    return result.stdout.strip()


def notifier(arguments: list[str]) -> str:
    script = Path.home() / ".claude/scripts/message/notifier.sh"
    return command("notifier", ["zsh", str(script), *arguments])


def remove_updates(owner: staff.StaffRow) -> None:
    """Remove the generated schedule files and retain reminders and history."""
    _ = notifier(["remove", f"showrunner-{owner.id}"])
    root = state_root()
    (root / "counts" / owner.id).unlink(missing_ok=True)


def instance(name: str) -> InstancePresent | InstanceAbsent:
    script = Path.home() / ".claude/scripts/message/notifier.sh"
    result = subprocess.run(["zsh", str(script), "status", name], capture_output=True, text=True, check=False)
    if result.returncode == 0:
        return InstancePresent(result.stdout.strip())
    if "no instance" in result.stdout + result.stderr or "no such instance" in result.stdout + result.stderr:
        return InstanceAbsent()
    raise RegistrationFailure("notifier", f"status: {failure_detail(result)}")


class ScheduledUpdates(NamedTuple):
    minutes: int
    aligned: bool


DEFAULT_MINUTES = 15


class UpdatesOnDemand(NamedTuple):
    """The document has no scheduled update interval."""


def update_schedule(lines: list[str]) -> ScheduledUpdates | UpdatesOnDemand:
    line = next((line for line in lines if line.startswith("- **Updates:**")), "")
    match = re.search(r"every (\d+) minutes", line)
    # Quiet by default (user, 2026-10-13): only a line asking for a schedule makes one.
    if match is None or re.search(r"\bon demand\b", line):
        return UpdatesOnDemand()
    minutes = int(match.group(1))
    if minutes <= 0:
        raise RegistrationFailure("prompt", "Updates interval must be positive")
    return ScheduledUpdates(minutes, "on the hour" in line)


def scheduled_prompt(minutes: int, production: Production) -> str:
    source = Path(__file__).resolve().parents[2] / "commands/showrunner/produce.md"
    document = source.read_text(encoding="utf-8")
    if "The prompt:\n" not in document:
        raise RegistrationFailure("register", "The prompt: marker is missing")
    tail = document.split("The prompt:\n", 1)[1]
    if "\n**A tick**" not in tail:
        raise RegistrationFailure("register", "**A tick** marker is missing")
    template = tail.split("\n**A tick**", 1)[0]
    prompt = "\n".join(line.removeprefix("> ").removeprefix(">") for line in template.strip().splitlines())
    prompt = prompt.replace("<N>", str(minutes)).replace("<zone>", str(production.zone)).replace("<showrunner name>", production.name)
    prompt = prompt.replace("<STATE_DIR>", str(showrunners.session_state_dir(production.owner.id)))
    if not prompt.strip():
        raise RegistrationFailure("prompt", "scheduled prompt is empty")
    return prompt


def adopt_moved_state(production: Production, *, record: staff.StaffSnapshot | None = None) -> list[str]:
    """Copy missing scratchpad state."""
    done: list[str] = []
    try:
        record = staff.snapshot() if record is None else record
        identity = production.owner.id
        copied = showrunners.adopt_session_state(identity, record=record)
        if copied:
            done.append(f"copied {', '.join(copied)} to {showrunners.session_state_dir(identity)}")
    except (OSError, UnicodeError, RegistrationFailure, staff.StaffRefused, staff.StaffUnreadable) as error:
        done.append(f"state move skipped: {error}")
    return done


def status_line(lines: list[str]) -> re.Match[str] | None:
    return next((re.search(r"^> \*\*Status: PRODUCTION — (planned|running|wrapped)\.\*\*", line)
                 for line in lines if line.startswith("> **Status:")), None)


def register(production: Production) -> None:
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not session_id:
        raise RegistrationFailure("register", "CLAUDE_CODE_SESSION_ID is required")
    require_admission(production)
    try:
        agents_log.append(session_id, "showrunner", None, datetime.now(timezone.utc))
    except Exception as exc:
        # Recording must never interrupt registration.
        print("agents_log: " + " ".join(str(exc).splitlines()), file=sys.stderr)
    paths = resolve_production_paths(production.doc)
    if isinstance(paths, EnrolledPaths):
        for path in (paths.live_doc, paths.log):
            require_untracked_ignored(paths.main, path.relative_to(paths.main).as_posix())
    else:
        relative = production.doc.relative_to(production.checkout)
        dirty = command("register", ["git", "status", "--porcelain", "--", str(relative)], cwd=production.checkout)
        if dirty:
            raise RegistrationFailure("register", "the production doc has uncommitted edits; commit or discard them first")
    for line in adopt_moved_state(production):
        report("state-dir", "ok", line)
    # The staff row owns the timer.
    owner = production.owner
    updates = f"showrunner-{owner.id}"
    _ = notifier(["new", updates, "--to", f"session:{owner.id}", "--every", str(DEFAULT_MINUTES),
                  "--prompt", "scheduled"])
    _ = notifier(["stop", updates])
    if isinstance(instance("stall-watch"), InstanceAbsent):
        run = f"{Path.home()}/.claude/scripts/lib/py {Path.home()}/.claude/scripts/production/stall_watch.py"
        _ = notifier(["new", "stall-watch", "--every", "1", "--run", run])
    report("register", "ok", f"{updates} registered; automatic reports are off")


def production_time(production: Production, stamp: str) -> str:
    zone = production.zone
    if re.fullmatch(r"\d{1,2}:\d{2}", stamp):
        today = datetime.now(zone).date()
        hour, minute = (int(part) for part in stamp.split(":"))
        moment = datetime(today.year, today.month, today.day, hour, minute, tzinfo=zone)
    else:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        moment = moment.replace(tzinfo=zone) if moment.tzinfo is None else moment.astimezone(zone)
    return moment.strftime("%H:%M %Z")


def state_error(step: str, path: Path, key: str, expected: str) -> RegistrationFailure:
    return RegistrationFailure(step, f"{path}: {key} must be {expected}")


def judgment_state(path: Path | None, step: str) -> ShowrunnerState:
    if path is None:
        return ShowrunnerState((), (), KeepOutstanding())
    value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(value, dict):
        raise state_error(step, path, "root", "an object")
    fields = cast(dict[str, object], value)
    for key in fields:
        if key not in {"units", "merges_held", "open_for_user", "report_to_user", "next"}:
            raise state_error(step, path, key, "a known key")
    raw_units = fields.get("units", [])
    if not isinstance(raw_units, list):
        raise state_error(step, path, "units", "a list")
    units: list[UnitJudgment] = []
    for index, raw in enumerate(cast(list[object], raw_units)):
        key = f"units[{index}]"
        if not isinstance(raw, dict):
            raise state_error(step, path, key, "an object")
        unit = cast(dict[str, object], raw)
        for field in unit:
            if field not in {"unit", "phase", "wait"}:
                raise state_error(step, path, f"{key}.{field}", "a known key")
        for field in ("unit", "phase", "wait"):
            if field in unit and not isinstance(unit[field], str):
                raise state_error(step, path, f"{key}.{field}", "text")
        units.append(UnitJudgment(cast(str, unit.get("unit", "")),
                                  cast(str, unit.get("phase", "not stated")),
                                  cast(str, unit.get("wait", "not stated"))))
    raw_held = fields.get("merges_held", [])
    if not isinstance(raw_held, list) or not all(isinstance(item, str) for item in cast(list[object], raw_held)):
        raise state_error(step, path, "merges_held", "a list of text")
    raw_next = fields.get("next", [])
    if not isinstance(raw_next, list) or not all(isinstance(item, str) and item.strip() and "\n" not in item
                                                 for item in cast(list[object], raw_next)):
        raise state_error(step, path, "next", "a list of one-line text")
    outstanding: KeepOutstanding | ReplaceOutstanding = KeepOutstanding()
    if "open_for_user" in fields:
        raw_open = fields["open_for_user"]
        if not isinstance(raw_open, list):
            raise state_error(step, path, "open_for_user", "a list")
        items: list[OpenForUserItem] = []
        for index, raw in enumerate(cast(list[object], raw_open)):
            key = f"open_for_user[{index}]"
            if isinstance(raw, str):
                if not raw.strip():
                    raise state_error(step, path, key, "nonempty text")
                items.append(OpenReminder(raw, ""))
                continue
            if not isinstance(raw, dict):
                raise state_error(step, path, key, "text or an object")
            item = cast(dict[str, object], raw)
            for field in item:
                if field not in {"text", "after", "question", "urgency", "due", "follows"}:
                    raise state_error(step, path, f"{key}.{field}", "a known key")
            rank = item_ranking(item, path, step, key)
            if not isinstance(item.get("text"), str) or not cast(str, item["text"]).strip():
                raise state_error(step, path, f"{key}.text", "nonempty text")
            after = item.get("after", "")
            if not isinstance(after, str):
                raise state_error(step, path, f"{key}.after", "YYYY-MM-DDTHH:MM")
            try:
                _ = question_path.read_legacy_later(after or None)
            except ValueError:
                raise state_error(step, path, f"{key}.after", "YYYY-MM-DDTHH:MM") from None
            if "question" in item:
                question = item["question"]
                if not isinstance(question, str) or not question.strip():
                    raise state_error(step, path, f"{key}.question", "nonempty text")
                items.append(QuestionForReview(cast(str, item["text"]), after, question, rank))
            else:
                items.append(OpenQuestionWithoutText(cast(str, item["text"]), after, rank))
        outstanding = ReplaceOutstanding(tuple(items))
    return ShowrunnerState(tuple(units), tuple(cast(list[str], raw_held)), outstanding,
                           report_items(fields, path, step), tuple(cast(list[str], raw_next)))


def item_ranking(item: dict[str, object], path: Path, step: str, key: str) -> Ranking:
    """An item's `urgency` (user, blocks, soon, normal or later), `due` (with soon) and `follows`
    (the text of the item it comes after); none of them is a normal item."""
    follows = item.get("follows", "")
    if not isinstance(follows, str) or "\n" in follows:
        raise state_error(step, path, f"{key}.follows", "one line of text")
    level, due = item.get("urgency", ""), item.get("due", "")
    if not isinstance(level, str) or not isinstance(due, str):
        raise state_error(step, path, f"{key}.urgency", "text")
    try:
        return Ranking(urgency.read(level, due), follows.strip())
    except urgency.UrgencyError:
        raise state_error(step, path, f"{key}.urgency", "user, blocks, soon with `due`, normal or later") from None


def report_items(fields: dict[str, object], path: Path, step: str) -> KeepReports | ReplaceReports:
    """`report_to_user`: what the user asked to be told about, each until an ISO time."""
    if "report_to_user" not in fields:
        return KeepReports()
    raw_reports = fields["report_to_user"]
    if not isinstance(raw_reports, list):
        raise state_error(step, path, "report_to_user", "a list")
    reports: list[ReportItem] = []
    for index, raw in enumerate(cast(list[object], raw_reports)):
        key = f"report_to_user[{index}]"
        if not isinstance(raw, dict):
            raise state_error(step, path, key, "an object")
        item = cast(dict[str, object], raw)
        for field in item:
            if field not in {"text", "until"}:
                raise state_error(step, path, f"{key}.{field}", "a known key")
        text, until = item.get("text"), item.get("until")
        if not isinstance(text, str) or not text.strip() or "\n" in text:
            raise state_error(step, path, f"{key}.text", "one nonempty line of text")
        if not isinstance(until, str):
            raise state_error(step, path, f"{key}.until", "an ISO time")
        try:
            _ = datetime.fromisoformat(until)
        except ValueError:
            raise state_error(step, path, f"{key}.until", "an ISO time") from None
        reports.append(ReportItem(text=text, until=until))
    return ReplaceReports(tuple(reports))


def outstanding_file(reference: str, *, identities: QuestionStaff | None = None) -> Path:
    root = state_root()
    identities = identities if identities is not None else QuestionStaff.read()
    member = question_owner(reference, identities.record)
    identity = member.id if member is not None and member.kind == "producer" else showrunner_id(reference, record=identities.record)
    return root / "outstanding" / f"{identity}.json"


def outstanding_path(production: QuestionScope) -> Path:
    return outstanding_path_for_owner(production.owner.id)


def outstanding_path_for_owner(owner: staff.StaffId) -> Path:
    return state_root() / "outstanding" / f"{owner}.json"


@dataclass(frozen=True)
class QuestionFolder:
    """The question folder and its fixed production owner."""

    path: Path
    owner: staff.StaffId


@dataclass(frozen=True)
class QuestionOwnerUnavailable:
    """The document has no local showrunner row to own its questions."""

    document: Path


def question_folder(document: Path | staff.StaffRow, identities: QuestionStaff, *,
                    lines: list[str] | None = None) -> QuestionFolder | QuestionOwnerUnavailable:
    """Select the production's question folder from its document and staff context."""
    if isinstance(document, staff.StaffRow):
        if document.kind != "producer":
            raise RegistrationFailure("questions", f"{document.id}: expected a producer")
        return QuestionFolder(record_store.default_question_folder(document.id), document.id)
    owner = identities.record.by_doc(local_machine(), document)
    if owner is None or owner.kind != "showrunner":
        return QuestionOwnerUnavailable(document)
    assert isinstance(owner.doc, (staff.LiveDoc, staff.WrappedDoc))
    folder = issues_folder(owner.doc.path, lines=lines)
    return QuestionFolder(folder if folder is not None else record_store.default_question_folder(owner.id), owner.id)


@dataclass(frozen=True)
class ParseQuestionFromFields:
    """Parse a Question that a writer constructed in memory."""


@dataclass(frozen=True)
class QuestionRecord:
    """A question issue with a fixed asker and its existing queue fields."""

    path: Path
    owner: staff.StaffId
    asker: staff.StaffId
    item: OutstandingItem
    status: str
    read_snapshot: Outstanding | ParseQuestionFromFields = field(
        default=ParseQuestionFromFields(), repr=False, compare=False)

    def parsed_snapshot(self) -> Outstanding:
        """Reuse the parse from the record read."""
        if isinstance(self.read_snapshot, ParseQuestionFromFields):
            return parse_outstanding_entries([self.item], self.path)[0]
        return self.read_snapshot


def question_records(production: QuestionScope, identities: QuestionStaff, *, strict: bool = False) -> list[QuestionRecord]:
    return question_records_for_document(question_source(production), identities, strict=strict)


def question_records_for_document(doc: Path | staff.StaffRow, identities: QuestionStaff, *, include_unrouted: bool = False,
                                  lines: list[str] | None = None,
                                  sources: list[Path] | None = None, statuses: tuple[str, ...] = (),
                                  owner_ids: set[staff.StaffId] | None = None, strict: bool = False) -> list[QuestionRecord]:
    """Read canonical question records without requiring unrelated checkpoint fields."""
    try:
        folder = question_folder(doc, identities, lines=lines)
    except (OSError, UnicodeError):
        if strict or sources is not None:
            raise
        assert isinstance(doc, Path)
        row = identities.record.by_doc(local_machine(), doc)
        folder = (QuestionFolder(record_store.default_question_folder(row.id), row.id)
                  if row is not None and row.kind == "showrunner" else QuestionOwnerUnavailable(doc))
    if isinstance(folder, QuestionOwnerUnavailable):
        try:
            source = issues_folder(folder.document, lines=lines) if include_unrouted else None
        except OSError:
            source = None
        if source is None:
            return []
    else:
        source = folder.path
    owner = folder.owner if isinstance(folder, QuestionFolder) else staff.StaffId("")
    require_complete = strict or sources is not None
    roots = record_store.known_folders(record_store.RecordKind.QUESTION, owner, (source,),
                                       document=doc if isinstance(doc, Path) else None, strict=require_complete)
    paths = record_store.stored_paths(roots, sources=sources, strict=require_complete)
    if sources is not None:
        # Use the Question filename in the shared current folder. The proposal reader reads its other headers.
        shared = source.resolve() if isinstance(doc, Path) and owner and source != record_store.default_question_folder(owner) else None
        paths = (path for path in paths if path.parent != shared or path.name.startswith("question-"))
    return selected_question_records(sorted(paths), statuses, owner_ids, sources, owner, strict=strict)


def review_question_path(folder: QuestionFolder, request_id: str) -> Path:
    digest = hashlib.sha256(request_id.encode()).hexdigest()
    return folder.path / f"question-{folder.owner}-review-{digest}.md"


def find_review_question_in_folder(folder: QuestionFolder,
                                   lookup: new_item_dialog.ReviewSubmissionLookup,
                                   *, other_folders: Iterable[Path] = (),
                                   document: Path | None = None) -> QuestionRecord | None:
    paths = _known_question_paths(folder.owner, folder.path, *other_folders, document=document,
                                  strict=True, require_document=False)
    for path in paths:
        fields = _question_fields(path, folder.owner)
        if fields is None:
            continue
        registration = fields.value("registration")
        if isinstance(lookup, new_item_dialog.ByRequestId) and lookup.value not in registration:
            continue
        if isinstance(lookup, new_item_dialog.ByItemId) and _stored_question_id(fields) != lookup.value:
            continue
        for record in _question_records([fields], legacy_owner=folder.owner):
            if "review_submission" in record.item and (isinstance(lookup, new_item_dialog.ByItemId)
                    or new_item_dialog.read_submission(record.item["review_submission"]).request_id == lookup.value):
                return record
    return None


def read_review_question(path: Path, owner: str = "") -> QuestionRecord:
    """Read one sent question at its native path."""
    records = _question_records([Issue(path, record_store.read_header(path, strict=True))], legacy_owner=owner)
    if len(records) != 1 or "review_submission" not in records[0].item:
        raise OSError(f"Cannot read a saved question at {path}.")
    return records[0]


def selected_question_records(paths: list[Path], statuses: tuple[str, ...], owner_ids: set[staff.StaffId] | None,
                              sources: list[Path] | None = None, owner: str = "", *, strict: bool = True,
                              allowed_owners: tuple[staff.StaffId, ...] = ()) -> list[QuestionRecord]:
    """Select by status and owner before decoding the question registration."""
    def wanted(issue: Issue) -> bool:
        production = record_store.record_owner(record_store.RecordKind.QUESTION, issue, owner)
        if issue.value("record_type") != "question":
            return False
        if allowed_owners and production not in allowed_owners or owner and production != owner:
            return False
        if owner_ids is not None:
            owner_ids.add(production)
        return not statuses or issue.value("status") in statuses or issue.value("status") not in ("open", "closed")

    issues: list[Issue] = []
    for path in paths:
        keys = frozenset(("record_type", "status", "production_id"))
        if strict:
            fields = record_store.read_header(path, sources=sources, strict=True)
            if not wanted(Issue(path, fields)):
                continue
        else:
            fields = record_store.read_header(path, sources=sources, keys=keys, select=wanted)
        if fields:
            issues.append(Issue(path, fields))
    return _question_records(issues, legacy_owner=owner)


def question_records_for_owner(owner: staff.StaffId, *, statuses: tuple[str, ...] = (),
                               owner_ids: set[staff.StaffId] | None = None, strict: bool = False) -> list[QuestionRecord]:
    paths = sorted(_known_question_paths(owner, strict=strict))
    return selected_question_records(paths, statuses, owner_ids, owner=owner, strict=strict)


def unowned_question_records(identities: QuestionStaff, *, statuses: tuple[str, ...] = (),
                             owner_ids: set[staff.StaffId] | None = None) -> list[QuestionRecord]:
    """Read state questions outside the stores of showrunners."""
    records: list[QuestionRecord] = []
    for owner, row in record_store.store_owners(record_store.RecordKind.QUESTION, identities.record).items():
        if row is None or row.kind != "showrunner":
            records.extend(question_records_for_owner(owner, statuses=statuses, owner_ids=owner_ids))
    return records


def _question_records(issues: list[Issue], *, legacy_owner: str = "") -> list[QuestionRecord]:
    """Read canonical question fields from their issue records."""
    records: list[QuestionRecord] = []
    for issue in issues:
        if issue.value("record_type") != "question":
            continue
        path = issue.path
        try:
            raw = cast(object, json.loads(issue.value("registration")))
        except ValueError as error:
            raise RegistrationFailure("questions", f"{path}: invalid question registration: {error}") from error
        if not isinstance(raw, dict):
            raise RegistrationFailure("questions", f"{path}: invalid question registration")
        fields = cast(dict[str, object], raw)
        parsed, = parse_outstanding_entries([fields], path)
        item = cast(OutstandingItem, cast(object, fields))
        asker = item.get("asked_by", "")
        if not asker or asker != issue.value("asked_by"):
            raise RegistrationFailure("questions", f"{path}: invalid question asker")
        status = issue.value("status")
        if status not in {"open", "closed"}:
            raise RegistrationFailure("questions", f"{path}: question status must be open or closed")
        owner = record_store.record_owner(record_store.RecordKind.QUESTION, issue, legacy_owner)
        records.append(QuestionRecord(path, owner, staff.StaffId(asker), item, status, parsed))
    return records


def pending_review_reply(record: QuestionRecord) -> bool:
    """Return whether a sent Question still accepts a scoped reply."""
    return record.status == "open" and "review_submission" in record.item and "followup" in record.item


@contextmanager
def wrap_question_locks(*owners: staff.StaffId) -> Generator[None]:
    """Lock Question stores in staff ID order."""
    with ExitStack() as locks:
        for owner in sorted(set(owners)):
            _ = locks.enter_context(outstanding_lock(outstanding_path_for_owner(owner)))
            _ = locks.enter_context(_question_number_lock(owner))
        yield


def close_questions(document: Path, identities: QuestionStaff) -> None:
    """Close the production's questions and retain their records for history."""
    owner = identities.owner(document).id
    with outstanding_lock(outstanding_file(owner, identities=identities)), _question_number_lock(owner), ExitStack() as rollback:
        closing = [replace(record, status="closed") for record in question_records_for_document(document, identities, strict=True)
                   if record.status == "open" and not pending_review_reply(record)]
        texts = [_question_text(record) for record in closing]
        with question_numbers() as numbers:
            for record, text in zip(closing, texts, strict=True):
                _ = rollback.enter_context(record_store.keep_file_on_error(record.path))
                _write_question_text(record, text, numbers)


def _question_fields(path: Path, owner: staff.StaffId) -> Issue | None:
    issue = Issue(path, record_store.read_header(path, strict=True))
    if issue.value("record_type") == "question" and record_store.record_owner(record_store.RecordKind.QUESTION, issue, owner) == owner:
        return issue
    return None


def _stored_question_id(issue: Issue) -> str:
    registration = issue.value("registration")
    try:
        item = cast(object, json.loads(registration))
    except ValueError:
        # Reserve a legible ID even if a different field is damaged.
        match = re.search(r'(?:^|[,{])\s*"review_id"\s*:\s*"(Q[1-9][0-9]*)"', registration)
        return match[1] if match else ""
    if isinstance(item, dict):
        identity = cast(dict[object, object], item).get("review_id")
        if isinstance(identity, str) and re.fullmatch(r"Q[1-9][0-9]*", identity):
            return identity
    return ""


def _question_ids(owner: staff.StaffId, paths: Iterable[Path]) -> Iterator[tuple[Issue, str]]:
    for path in paths:
        if (fields := _question_fields(path, owner)) is not None and (identity := _stored_question_id(fields)):
            yield fields, identity


def _known_question_paths(owner: staff.StaffId, *folders: Path, document: Path | None = None,
                          strict: bool = False, require_document: bool = True) -> Iterator[Path]:
    roots = record_store.known_folders(record_store.RecordKind.QUESTION, owner, folders, document=document,
                                       strict=strict, require_document=require_document)
    return record_store.stored_paths(roots, strict=strict)


def _question_number_lock(owner: staff.StaffId) -> AbstractContextManager[None]:
    return outstanding_lock(state_root() / "review-folders" / owner / "question-numbers")


def _seed_question_id(path: Path) -> str:
    fields = record_store.number_fields(path, frozenset(("record_type", "registration")))
    return _stored_question_id(fields) if fields.value("record_type") == "question" else ""


def question_numbers() -> AbstractContextManager[record_store.LockedRecordNumbers]:
    return record_store.record_numbers(record_store.RecordKind.QUESTION, _seed_question_id)


def _publish_question(record: QuestionRecord, *, record_history: Callable[[], None] | None = None) -> str:
    """Save the record and assign an ID if this is its first numbered write."""
    with question_numbers() as numbers:
        identity = _numbered_question_id(record) or numbers.next_id(record.path.parent)
        numbered = replace(record, item={**record.item, "review_id": identity})
        text = _question_text(numbered)
        if record_history is not None:
            record_history()
        _write_question_text(numbered, text, numbers)
        record.item["review_id"] = identity
        return identity


def _numbered_question_id(record: QuestionRecord) -> str:
    identity = record.item.get("review_id", "")
    return identity if re.fullmatch(r"Q[1-9][0-9]*", identity) else ""


def _current_legacy_question(record: QuestionRecord) -> QuestionRecord:
    """Check that an unnumbered selection still refers to the same Question."""
    fields = _question_fields(record.path, record.owner)
    if fields is None:
        raise RegistrationFailure("questions", f"{record.path}: the selected Question changed")
    current, = _question_records([fields], legacy_owner=record.owner)
    previous = current.item.copy()
    _ = previous.pop("review_id", None)
    expected = record.item.copy()
    _ = expected.pop("review_id", None)
    if replace(current, item=previous) != replace(record, item=expected):
        raise RegistrationFailure("questions", f"{record.path}: the selected Question changed")
    return current


def _file_question(record: QuestionRecord,
                   before_write: Callable[[str, Path], None] = lambda _id, _path: None
                   ) -> QuestionRecord:
    with _question_number_lock(record.owner), question_numbers() as numbers:
        folder = record.path.parent
        names = {path.name for path in _known_question_paths(record.owner, folder, strict=True)}
        identity = numbers.next_id(folder)
        while True:
            path = record_store.free_record_path(record.path, names)
            filed = replace(record, path=path, item={**record.item, "review_id": identity})
            text = _question_text(filed, new_record=True)
            assert text is not None
            try:
                with numbers.write(path, identity, record.owner, new_record=True,
                                   before_write=lambda: before_write(identity, path)):
                    try:
                        record_store.create_record_text(path, text)
                    except record_store.ReusedReviewRecord:
                        filed = read_review_question(path, record.owner)
                        if filed.owner != record.owner or not (saved_id := _numbered_question_id(filed)):
                            raise OSError(f"Cannot read a saved question at {path}.") from None
                        before_write(saved_id, path)
            except record_store.OccupiedRecordPath:
                names.add(path.name)
                identity = numbers.next_id_after_collision(identity, path)
                continue
            return filed


def write_question_record(record: QuestionRecord, *, assign_id: bool = False,
                          before_write: Callable[[], None] | None = None) -> None:
    if assign_id and not _numbered_question_id(record):
        with _question_number_lock(record.owner):
            _ = _publish_question(record, record_history=before_write)
        return
    text = _question_text(record)
    if before_write is not None:
        before_write()
    _write_question_text(record, text)


def _write_question_text(record: QuestionRecord, text: str | None,
                         numbers: record_store.LockedRecordNumbers | None = None) -> None:
    if text is None:
        return
    identity = _numbered_question_id(record)
    if not identity:
        write_text_atomic(record.path, text)
        return
    sequence = nullcontext(numbers) if numbers is not None else question_numbers()
    with sequence as locked, locked.write(record.path, identity, record.owner):
        write_text_atomic(record.path, text)


def _question_text(record: QuestionRecord, *, new_record: bool = False) -> str | None:
    item = record.item
    stored = _question_records([Issue(record.path, record_store.read_header(record.path, strict=True))],
                               legacy_owner=record.owner) if not new_record and record.path.exists() else []
    old = stored[0].item if stored else None
    old_id = old.get("review_id", "") if old is not None else ""
    identity = item.get("review_id", "")
    if stored and (stored[0].owner != record.owner or old_id and identity and old_id != identity):
        raise RegistrationFailure("questions", f"{record.path}: the selected Question changed")
    old_text = old["text"] if old is not None else None
    old_ask = old["parts"]["ask"] if old is not None and "parts" in old else None
    titles = {"text": item["text"]}
    if "parts" in item:
        titles["ask"] = item["parts"]["ask"]
    for field, title in titles.items():
        previous = old_text if field == "text" else old_ask
        if title != previous:
            checked = check_question_title(title)
            if isinstance(checked, RefusedQuestionTitle):
                raise RegistrationFailure("questions", f"{field}: {checked.reason}")
    if "waiting_why" not in item:
        if stored and stored[0].item.get("waiting_why"):
            item["waiting_why"] = stored[0].item.get("waiting_why", "")
    if "review_id" not in item:
        if stored and stored[0].item.get("review_id"):
            item["review_id"] = stored[0].item.get("review_id", "")
    if "review_log" not in item:
        if stored and stored[0].item.get("review_log"):
            item["review_log"] = stored[0].item.get("review_log", "")
    if "question" not in item:
        raise RegistrationFailure("questions", f"{record.path}: the question text is missing")
    value = item.get("review_submission")
    if value is not None and "reply_token" in value:
        details = new_item_dialog.read_submission(value)
        item = item.copy()
        item["review_submission"] = new_item_dialog.hashed_submission_fields(value, details)
    if stored and replace(record, item=item) == stored[0]:
        return None
    return "\n".join([
        "---", "record_type: question", "tags: [question]", f"status: {record.status}",
        f"production_id: {record.owner}", f"asked_by: {record.asker}",
        "registration: " + json.dumps(item, ensure_ascii=False), "---", "",
        f"# {item['text']}", "", item["question"], "",
    ])


def write_review_submission(record: QuestionRecord, fields: dict[str, object]) -> QuestionRecord:
    """Replace submission fields under the Question locks."""
    item = record.item.copy()
    item["review_submission"] = fields
    path = record.path
    with path.open(encoding="utf-8", newline="") as stream:
        text = stream.read()
    end = text.index("\n") + 1
    lines = record_store.lines_with_endings(text[end:])
    for index, line in enumerate(lines):
        if line.rstrip("\r\n") == "---":
            break
        if line.startswith("registration:"):
            newline = line[len(line.rstrip("\r\n")):]
            lines[index] = "registration: " + json.dumps(item, ensure_ascii=False) + newline
            _write_question_text(record, text[:end] + "".join(lines))
            return replace(record, item=item)
    raise RegistrationFailure("questions", f"{path}: the registration field is missing")


def scrub_reply_token(path: Path, owner: staff.StaffId) -> bool:
    with wrap_question_locks(owner):
        record = read_review_question(path, owner)
        if record.owner != owner:
            raise RegistrationFailure("questions", f"{path}: the question owner changed")
        value = record.item.get("review_submission")
        if value is None or "reply_token" not in value:
            return False
        details = new_item_dialog.read_submission(value)
        _ = write_review_submission(record, new_item_dialog.hashed_submission_fields(value, details))
        return True


def record_review_log(source: Path | QuestionRecord, words: str, who: str = "nate", *,
                      owner: staff.StaffId | None = None, check: Callable[[], object] | None = None) -> None:
    """Keep the user's action on the canonical question record."""
    if isinstance(source, Path):
        fields = record_store.read_header(source, strict=True)
        record, = _question_records([Issue(source, fields)], legacy_owner=owner or "")
    else:
        record = source
    if owner is not None and record.owner != owner:
        raise RegistrationFailure("questions", f"{record.path}: the question owner changed")
    with (outstanding_lock(state_root() / "outstanding" / f"{record.owner}.json"), _question_number_lock(record.owner)):
        if check is not None:
            _ = check()
        record = _current_question(record, "questions")
        events = review_events.decode(record.item.get("review_log", ""))
        event = review_events.ReviewLog(datetime.now().astimezone(user_zone()), who, words)
        record.item["review_log"] = review_events.encode(review_events.append(events, event))
        _ = _publish_question(record)


def store_questions(production: QuestionScope, entries: list[OutstandingItem], identities: QuestionStaff, *,
                    write_list: Callable[[], None] = lambda: None, last: tuple[OutstandingItem, ...] = ()) -> None:
    """Check all record changes. Save the new Ask after its list and legacy records."""
    final_items = {id(entry) for entry in last}
    questions = [entry for entry in entries if "question" in entry and id(entry) not in final_items]
    questions.extend(last)
    existing = native_question_records(production, identities, strict=True)
    if not questions and not existing:
        write_list()
        return
    document = question_source(production)
    folder = question_folder(document, identities)
    if isinstance(folder, QuestionOwnerUnavailable):
        raise RegistrationFailure("questions", f"{folder.document}: no showrunner staff row")
    owner = folder.owner
    current = {record.item['review_id']: record for record in existing if record.status == 'open' and 'review_id' in record.item}
    legacy = {(record.asker, record.item['text']): record for record in existing
              if record.status == 'open' and 'review_id' not in record.item}
    with (_question_number_lock(owner),
          question_numbers() as numbers,
          ExitStack() as rollback):
        retained: set[Path] = set()
        plans: list[tuple[QuestionRecord, str | None]] = []
        names = {path.name for path in _known_question_paths(owner, folder.path, strict=True)}
        ceiling = numbers.high_water

        def plan(record: QuestionRecord) -> None:
            nonlocal ceiling
            identity = _numbered_question_id(record)
            if not identity:
                ceiling = max(ceiling, numbers.floor(record.path.parent)) + 1
                record = replace(record, item={**record.item, "review_id": f"Q{ceiling}"})
            ceiling = max(ceiling, numbers.number(_numbered_question_id(record)))
            plans.append((record, _question_text(record)))

        for entry in questions:
            item = entry.copy()
            row = question_member(production, item, identities)
            item["asked_by"] = row.id
            selected = current.get(item.get("review_id", "")) or legacy.get((row.id, item["text"]))
            if selected is None:
                digest = hashlib.sha256(f"{owner}:{row.id}:{item['text']}".encode()).hexdigest()
                path = record_store.free_record_path(folder.path / f"question-{row.id}-{digest[:16]}.md", names)
                names.add(path.name)
                record = QuestionRecord(path, owner, row.id, item, "open")
            else:
                selected = _current_question(selected, "questions")
                if identity := _numbered_question_id(selected):
                    item["review_id"] = identity
                record = replace(selected, item=item)
            retained.add(record.path)
            plan(record)
        final = plans[len(plans) - len(last):]
        if last:
            del plans[-len(last):]
        for record in existing:
            if record.status == "open" and record.path not in retained:
                plan(replace(_current_question(record, "questions"), status="closed"))

        def write(record: QuestionRecord, text: str | None) -> None:
            if text is not None:
                _ = rollback.enter_context(record_store.keep_file_on_error(record.path))
                _write_question_text(record, text, numbers)

        for record, text in plans:
            write(record, text)
        write_list()
        for record, text in final:
            write(record, text)


def question_member(production: QuestionScope, item: OutstandingItem, identities: QuestionStaff) -> staff.StaffRow:
    reference = item.get("asked_by", "")
    row = identities.record.by_member_reference(reference) if reference else None
    if row is None:
        row = identities.asker(production, "" if reference == SHOWRUNNER_ASKER else reference)
    return row


def migrate_questions(production: Production, *, identities: QuestionStaff | None = None) -> None:
    """Move legacy question identities into issue records without changing queue keys."""
    identities = identities if identities is not None else QuestionStaff.read()
    path = outstanding_path(production)
    with outstanding_lock(path):
        legacy = legacy_entries(path)
        existing = open_entries(production, identities)
        recorded = {(record.asker, record.item["text"]) for record in native_question_records(production, identities, strict=True)}
        questions = {(question_member(production, entry, identities).id, entry["text"]): entry
                     for entry in existing if "question" in entry}
        for entry in legacy:
            if "question" in entry:
                key = (question_member(production, entry, identities).id, entry["text"])
                if key not in questions and key not in recorded:
                    questions[key] = entry
        reminders = [entry for entry in legacy if "question" not in entry]
        def write_list() -> None:
            if any("question" in entry for entry in legacy):
                write_text_atomic(path, json.dumps(reminders) + "\n")

        with record_store.keep_file_on_error(path):
            store_questions(production, [*reminders, *questions.values()], identities, write_list=write_list)
    report("migrate-questions", "ok", "recorded")


def questions_table(production: QuestionScope, *, identities: QuestionStaff | None = None) -> None:
    """Print question issues without including proposals or ordinary reminders."""
    identities = identities if identities is not None else QuestionStaff.read()
    records = [record for record in question_records(production, identities) if record.status == "open"]
    if not records:
        print("no open questions")
        return
    print("| Asked by | Question | Date | State |")
    print("| --- | --- | --- | --- |")
    now = datetime.now(production.zone).replace(tzinfo=None)
    for record in records:
        row = identities.record.by_member_reference(record.asker)
        label = identities.display(row)[1] if row is not None else f"unavailable staff {record.asker}"
        item = record.item
        state = "waiting for your answer"
        parsed = record.parsed_snapshot()
        later = outstanding_later(parsed.timing)
        if isinstance(later, datetime) and later > now:
            state = "deferred until " + item.get("after", "")
        if "review_unavailable" in item:
            state += "; review unavailable"
        title = parsed.reference_text if isinstance(parsed, SentOutstandingQuestion) else item["text"]
        cells = (label, title, item["since"], state)
        print("| " + " | ".join(" ".join(cell.split()).replace("|", "\\|") for cell in cells) + " |")


_held_outstanding_locks: ContextVar[frozenset[Path]] = ContextVar("held_outstanding_locks", default=frozenset())


@contextmanager
def outstanding_lock(outstanding: Path) -> Generator[None]:
    """Lock the owner's records. Reuse the lock in the same call context."""
    path = outstanding.with_suffix(".lock").resolve()
    held = _held_outstanding_locks.get()
    if path in held:
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        token = _held_outstanding_locks.set(held | {path})
        try:
            yield
        finally:
            _held_outstanding_locks.reset(token)


def legacy_entries(outstanding: Path) -> list[OutstandingItem]:
    """Read legacy entries only for migration and reminder filtering."""
    if not outstanding.exists():
        return []
    raw = cast(object, json.loads(outstanding.read_text(encoding="utf-8")))
    _ = parse_outstanding_entries(raw, outstanding)
    return cast(list[OutstandingItem], raw)


def stored_entries(outstanding: Path) -> list[OutstandingItem]:
    """Read reminders without treating legacy JSON questions as live records."""
    if not outstanding.exists():
        return []
    raw = cast(object, json.loads(outstanding.read_text(encoding="utf-8")))
    if not isinstance(raw, list):
        raise InputError(f"{outstanding}: expected a JSON list")
    reminders: list[object] = [cast(object, entry) for entry in cast(list[object], raw)
                 if not isinstance(entry, dict) or "question" not in entry]
    _ = parse_outstanding_entries(reminders, outstanding)
    return cast(list[OutstandingItem], reminders)


def open_entries(production: QuestionScope, identities: QuestionStaff) -> list[OutstandingItem]:
    """Read reminders and open question records from their owning stores."""
    return [*stored_entries(outstanding_path(production)),
            *(record.item.copy() for record in native_question_records(production, identities) if record.status == "open")]


def native_question_records(production: QuestionScope, identities: QuestionStaff, *, strict: bool = False) -> list[QuestionRecord]:
    """Read staff questions for registration and list replacement."""
    return [record for record in question_records(production, identities, strict=strict) if "review_submission" not in record.item]


class QuestionStoreUnreadable(InputError, ValueError):
    """The canonical question store or its reminder list is invalid."""


def read_open_fields(path: Path | None, *, production: QuestionScope | None = None,
                     identities: QuestionStaff | None = None, document: Path | staff.StaffRow | None = None,
                     sources: list[Path] | None = None) -> list[OutstandingItem]:
    """Read reminders and canonical questions. A source collector requires complete reads."""
    document = question_source(production) if production is not None else document
    if path is None and document is None:
        return []
    try:
        if path is not None and sources is not None:
            sources.append(path)
        entries = stored_entries(path) if path is not None else []
        identities = identities if identities is not None else QuestionStaff.read(sources=sources)
        if document is None:
            row = identities.record.by_id(staff.StaffId(cast(Path, path).stem))
            document = (row if row is not None and row.kind == "producer"
                        else row.doc.path if row is not None and isinstance(row.doc, (staff.LiveDoc, staff.WrappedDoc)) else None)
            if document is None:
                return entries
        if sources is not None and isinstance(document, Path):
            sources.append(document)
        entries.extend(record.item.copy() for record in question_records_for_document(document, identities, sources=sources, statuses=("open",))
                       if record.status == "open")
        return entries
    except (RegistrationFailure, InputError) as error:
        raise QuestionStoreUnreadable(str(error)) from error


def read_open_items(path: Path | None, *, production: QuestionScope | None = None,
                    identities: QuestionStaff | None = None, document: Path | staff.StaffRow | None = None,
                    sources: list[Path] | None = None) -> list[Outstanding]:
    """Parse open reminders and questions with the same source collector."""
    entries = read_open_fields(path, production=production, identities=identities, document=document, sources=sources)
    location = path or (document if isinstance(document, Path) else Path("questions"))
    return parse_outstanding_entries(entries, location)


def due(items: list[Outstanding], now: datetime) -> list[Outstanding]:
    """Return the items whose deferred time has passed."""
    return [item for item in items
            if isinstance(item.timing, DueNow) or (isinstance(item.timing, DeferredUntil) and item.timing.at <= now)]


def read_all_open_fields() -> list[OutstandingItem]:
    """Read local open fields with one staff context and skip unreadable stores."""
    root = state_root()
    paths = {path.stem: path for path in (root / "outstanding").glob("*.json")}
    fields: list[OutstandingItem] = []
    identities = QuestionStaff.for_record(staff.initial_snapshot())
    documents: dict[str, Path] = {row.id: cast(staff.LiveDoc | staff.WrappedDoc, row.doc).path
                                  for row in identities.documents().values()}
    for identity in sorted(set(paths) | set(documents)):
        try:
            fields.extend(read_open_fields(paths.get(identity), document=documents.get(identity), identities=identities))
        except (OSError, ValueError):
            continue
    return fields


def write_open_entries(production: QuestionScope, entries: list[OutstandingItem], identities: QuestionStaff, *,
                       last: tuple[OutstandingItem, ...] = (), before_list: Callable[[], None] = lambda: None) -> None:
    """Keep unrecorded legacy questions before replacing their JSON list."""
    path = outstanding_path(production)
    legacy = [entry for entry in legacy_entries(path) if "question" in entry]
    if legacy:
        recorded = {(record.asker, record.item["text"]) for record in native_question_records(production, identities)}
        supplied = {(question_member(production, entry, identities).id, entry["text"])
                    for entry in entries if "question" in entry}
        entries = list(entries)
        for entry in legacy:
            key = (question_member(production, entry, identities).id, entry["text"])
            if key not in recorded and key not in supplied:
                entries.append(entry)
                supplied.add(key)
    reminders = [entry for entry in entries if "question" not in entry]
    text = json.dumps(reminders) + "\n"

    exists = path.exists()
    change_list = bool(reminders or exists) and (not exists or path.read_text(encoding="utf-8") != text)

    def write_list() -> None:
        before_list()
        if change_list:
            write_text_atomic(path, text)

    with record_store.keep_file_on_error(path) if change_list else nullcontext():
        store_questions(production, entries, identities, write_list=write_list, last=last)


# An item can wait longer than a week, so the day is named in full.
WHEN = "%a %b %d %H:%M"


class OpenItem(NamedTuple):
    name: str
    item: Outstanding
    asker_label: str = ""

    def line(self) -> str:
        """The item in full, led by its production and any level it has."""
        text = self.item.text if isinstance(self.item, OutstandingReminder) else self.item.question
        if not isinstance(self.item, OutstandingReminder):
            text += "".join(f"\nFollow-up: {each.followup}\nReply: {each.reply}" for each in self.item.followups)
        if isinstance(self.item, SentOutstandingQuestion):
            text += "\n" + (self.item.key if isinstance(self.item.timing, AwaitingReply) else self.item.read_command())
        level = urgency.cell(self.item.rank.urgency)
        asker = f", asked by {self.asker_label}" if self.asker_label else ""
        return f"{self.name}{f' [{level}]' if level else ''}, since {self.item.since:{WHEN}}{asker}: {text}"


class OpenLists(NamedTuple):
    due: list[urgency.Entry[OpenItem]]
    deferred: list[OpenItem]
    unreadable: list[str]
    # Questions with a follow-up that waits for the asker's reply.
    awaiting: list[OpenItem]


def question_queue(production: QuestionScope, below: bool = False) -> str:
    """The queue name for the production question that /next displayed."""
    return question_queue_name(production.owner.id, below)


def question_queue_name(owner: str, below: bool = False) -> str:
    """The queue name of the questions that the showrunner with this staff id owns."""
    return urgency.scope_queue(f"question-{owner}", below)


def question_entry(owner: str, item: OutstandingItem, path: Path) -> urgency.Entry[OutstandingItem]:
    """The queue entry of one stored question, keyed as `open_lists` keys it."""
    return ranked_question_entry(owner, item, parse_outstanding_entries([item], path)[0])


def ranked_question_entry(owner: str, item: OutstandingItem, parsed: Outstanding) -> urgency.Entry[OutstandingItem]:
    key = parsed.key if isinstance(parsed, SentOutstandingQuestion) else item_key(owner, item["text"])
    return urgency.Entry(key, item, parsed.rank.urgency, parsed.since,
                         after=item_key(owner, parsed.rank.follows) if parsed.rank.follows else "")


def item_key(name: str, text: str) -> str:
    return f"{name}: {text}"


def open_lists(production: QuestionScope, below: bool = False, *, identities: QuestionStaff | None = None) -> OpenLists:
    """Read due items, deferred items and unreadable lists from the selected productions."""
    identities = identities if identities is not None else QuestionStaff.read()
    now = datetime.now(production.zone).replace(tzinfo=None)
    due: list[urgency.Entry[OpenItem]] = []
    deferred: list[OpenItem] = []
    awaiting: list[OpenItem] = []
    unreadable: list[str] = []
    documents = identities.documents()
    documents.update({row.name: row for row in identities.record.rows if row.kind == "producer"})
    for name in identities.names(production.name, below):
        if name not in documents:
            print(f"questions: {name}: ownership evidence is unavailable", file=sys.stderr)
            unreadable.append(name)
            continue
        try:
            items = read_open_items(outstanding_file(name, identities=identities),
                                    production=production if name == production.name else None, identities=identities)
        except (InputError, OSError, ValueError):
            unreadable.append(name)
            continue
        for item in items:
            label = ""
            if not isinstance(item, OutstandingReminder) and item.asked_by:
                asker = identities.record.by_member_reference(item.asked_by)
                label = identities.display(asker)[1] if asker is not None else item.asked_by
            opened = OpenItem(name, item, label)
            if isinstance(item.timing, DeferredUntil) and item.timing.at > now:
                deferred.append(opened)
            elif isinstance(item.timing, AwaitingReply):
                awaiting.append(opened)
            else:
                owner = documents[name].id
                key = item.key if isinstance(item, SentOutstandingQuestion) else item_key(owner, item.text)
                due.append(urgency.Entry(key, opened, item.rank.urgency,
                                         item.since, after=item_key(owner, item.rank.follows) if item.rank.follows else ""))
    deferred.sort(key=lambda found: (cast(DeferredUntil, found.item.timing).at, found.name))
    return OpenLists(urgency.queue_order(due, question_queue(production, below)), deferred, unreadable, awaiting)


def open_items(production: Production) -> None:
    """Print in full what waits on the user: this production's own items.

    Replies show only the count, so this list is what the showrunner gives when the user asks what
    the open questions are. Each line is led by the production that owns the item.
    """
    lists = open_lists(production)
    for number, entry in enumerate(lists.due, 1):
        print(f"{number}. {entry.item.line()}")
    for found in lists.deferred:
        key = f" ({found.item.key})" if isinstance(found.item, SentOutstandingQuestion) else ""
        print(f"deferred until {cast(DeferredUntil, found.item.timing).at:{WHEN}}: {found.name}: "
              + (found.item.text if isinstance(found.item, OutstandingReminder) else found.item.question) + key)
    for found in lists.awaiting:
        key = f" ({found.item.key})" if isinstance(found.item, SentOutstandingQuestion) else ""
        print(f"waiting for the asker's reply since {cast(AwaitingReply, found.item.timing).sent:{WHEN}}: {found.name}: "
              + found.item.text + key)
    for name in lists.unreadable:
        print(f"{name}: {UNREADABLE_LIST}")
    report("open", "ok", f"{len(lists.due)} due, {len(lists.deferred)} deferred"
           + (f", {len(lists.awaiting)} waiting for a reply" if lists.awaiting else "")
           + (f", {len(lists.unreadable)} unreadable" if lists.unreadable else ""))


def next_item(production: Production) -> None:
    """Print the one question /next shows: the one shown last until it is answered, else the first due."""
    lists = open_lists(production)
    shown = urgency.top(lists.due, question_queue(production))
    print(shown.item.line() if shown is not None else "no question is open")
    report("next", "ok", f"{max(len(lists.due) - 1, 0)} more due")


def mark_important(production: QuestionScope, words: str | ByQuestionKey) -> None:
    """Mark the selected item important."""
    _ = act_on_question(production, words, "mark-important", QuestionStaff.read())


# What `ask` records as the asker when the showrunner files its own question.
SHOWRUNNER_ASKER = "showrunner"


@dataclass(frozen=True)
class QuestionFromReview:
    words: str
    waiting_why: str
    details: new_item_dialog.ReviewSubmission
    before_write: Callable[[str, Path], None]


@dataclass(frozen=True)
class QuestionFromStaff:
    pass


_QUESTION_FROM_STAFF = QuestionFromStaff()

def _ask_recipient(production: QuestionScope, text: str, parts: QuestionParts,
                   identities: QuestionStaff, outgoing: QuestionFromReview) -> QuestionRecord:
    document = question_source(production)
    folder = question_folder(document, identities)
    if isinstance(folder, QuestionOwnerUnavailable):
        raise RegistrationFailure("ask", f"{folder.document}: no showrunner staff row")
    sent = datetime.now(production.zone).strftime("%Y-%m-%dT%H:%M")
    item = OutstandingItem(since=sent, text=text, question=outgoing.words, asked_by=folder.owner,
                           parts={**parts, "context": outgoing.words},
                           followup=PendingFollowup(text=outgoing.words, sent=sent),
                           waiting_why=outgoing.waiting_why,
                           review_submission=new_item_dialog.stored_submission(outgoing.details))
    path = review_question_path(folder, outgoing.details.request_id)
    record = QuestionRecord(path, folder.owner, folder.owner, item, "open")
    with outstanding_lock(outstanding_path(production)):
        return _file_question(record, outgoing.before_write)


@overload
def ask(production: QuestionScope, text: str, parts: QuestionParts, unit: str, rank: Ranking, *,
        identities: QuestionStaff | None = None,
        outgoing: QuestionFromStaff = _QUESTION_FROM_STAFF) -> None: ...


@overload
def ask(production: QuestionScope, text: str, parts: QuestionParts, unit: str, rank: Ranking, *,
        identities: QuestionStaff | None = None, outgoing: QuestionFromReview) -> QuestionRecord: ...


def ask(production: QuestionScope, text: str, parts: QuestionParts, unit: str, rank: Ranking, *,
        identities: QuestionStaff | None = None,
        outgoing: QuestionFromReview | QuestionFromStaff = _QUESTION_FROM_STAFF) -> QuestionRecord | None:
    """Review and file a question with its asker's fixed staff id; its parts keep their layout on the Questions page."""
    identities = identities if identities is not None else QuestionStaff.read()
    if isinstance(outgoing, QuestionFromReview):
        limit = conf.read_or_repository("production").whole("question_title_max_chars")
        title = text[:limit].rstrip()
        return _ask_recipient(production, title, {**parts, "ask": title}, identities, outgoing)
    asker = identities.asker(production, unit)
    for option, title in (("--text", text), ("--ask", parts["ask"])):
        checked = check_question_title(title)
        if isinstance(checked, RefusedQuestionTitle):
            raise RegistrationFailure("ask", f"{option} {checked.reason}")
    # A question with choices names at least two and the one to take; an open question has none.
    if parts["choices"] and (len(parts["choices"]) < 2 or not parts["recommend"].strip()):
        raise RegistrationFailure("ask", "a question with choices needs at least two --choice and one --recommend")
    question = question_text(parts)
    problem = plumbing(text)
    if problem is not None:
        raise RegistrationFailure("ask", f"{text}: {problem}")
    outstanding = outstanding_path(production)
    if any(entry["text"] == text for entry in open_entries(production, identities)):
        raise RegistrationFailure("ask", f"{text}: an item with this text is already open")
    # The review can take a minute, so it runs before the lock: `questions` is not held up by it.
    accepted = review_open_question(production.name, QuestionForReview(text, "", question, rank))
    if isinstance(accepted, HeldRegistrationQuestion):
        raise RegistrationFailure("ask", f"question held for review: {text}: {accepted.details}")
    now = datetime.now(production.zone)
    entry = OutstandingItem(since=now.strftime("%Y-%m-%dT%H:%M"), text=text, question=question,
                            asked_by=asker.id, parts=parts)
    if isinstance(accepted, ReviewedQuestion):
        entry["reviewed"] = accepted.reviewed
    else:
        entry["review_unavailable"] = accepted.reason
    store_ranking(entry, rank)
    with outstanding_lock(outstanding):
        entries = open_entries(production, identities)
        if any(stored["text"] == text for stored in entries):
            raise RegistrationFailure("ask", f"{text}: an item with this text is already open")
        if any("question" in stored and stored["text"] == text
               and question_member(production, stored, identities).id == asker.id
               for stored in legacy_entries(outstanding)):
            raise RegistrationFailure("ask", f"{text}: a legacy question from this asker is already open")
        entries.append(entry)
        with record_store.keep_file_on_error(outstanding.with_suffix(".history.jsonl")):
            write_open_entries(production, entries, identities, last=(entry,),
                               before_list=lambda: record_question_history(outstanding, [entry], now))
    filed = accepted.reviewed if isinstance(accepted, ReviewedQuestion) else question
    print("filed question: " + filed)
    report("ask", "ok", f"{text}: on the open-questions list of {production.name}, not shown to the user")
    return None


@dataclass(frozen=True)
class ByQuestionKey:
    key: str


@dataclass(frozen=True)
class StoredReminder:
    """A reminder and the fixed owner of its list."""

    owner: staff.StaffId
    item: OutstandingItem


@dataclass(frozen=True)
class NoTitleMatch:
    """The requested title does not identify an existing item."""


type OpenItemRecord = QuestionRecord | StoredReminder
type QuestionMutation = Literal["answer", "withdraw", "mark-important", "reply", "followup"]
type QuestionAction = QuestionMutation | Literal["questions"]


def open_item_key(record: OpenItemRecord) -> str:
    if isinstance(record, StoredReminder):
        identity = hashlib.sha256(json.dumps([record.item["since"], record.item["text"]]).encode()).hexdigest()
        return f"reminder/{record.owner}/{identity}"
    if "review_submission" in record.item:
        details = new_item_dialog.read_submission(record.item["review_submission"])
        return new_item_dialog.submission_key(new_item_dialog.SubmissionKind.QUESTION, record.owner,
                                             record.item.get("review_id", ""), details.request_id)
    identity = _numbered_question_id(record)
    return f"question/{record.owner}/{identity}" if identity else ""


def open_question_items(path: Path, document: Path | staff.StaffRow | None,
                        identities: QuestionStaff) -> list[OpenItemRecord]:
    """Read open items and sent Questions in earlier native folders."""
    owner = staff.StaffId(path.stem)
    try:
        items: list[OpenItemRecord] = [StoredReminder(owner, item) for item in stored_entries(path)]
        if document is not None:
            items.extend(question_records_for_document(document, identities, statuses=("open",)))
        else:
            paths = sorted(_known_question_paths(owner))
            items.extend(selected_question_records(paths, ("open",), None, owner=owner))
        return items
    except (RegistrationFailure, InputError) as error:
        raise QuestionStoreUnreadable(str(error)) from error


def resolve_question_title(words: str, records: list[OpenItemRecord], action: QuestionAction, *,
                           partial: bool = False, history: Sequence[AnsweredItem] = ()
                           ) -> OpenItemRecord | AnsweredItem | NoTitleMatch:
    """Resolve one title before checking what the selected item permits."""
    wanted = words.strip()

    def contains(item: OutstandingItem | AnsweredItem) -> bool:
        return bool(wanted) and (wanted in item["text"] or wanted in item.get("question", ""))

    matches: Sequence[OpenItemRecord | AnsweredItem] = (
        [record for record in records if record.item["text"] == wanted]
        or [item for item in history if item["text"] == wanted]
        or [record for record in records if partial and contains(record.item)]
        or [item for item in history if contains(item)]
    )
    if not matches:
        return NoTitleMatch()
    if len(matches) == 1:
        return matches[0]
    message = f"{len(matches)} open items match {wanted!r}"
    keys = [key for record in matches if not isinstance(record, dict) and (key := open_item_key(record))]
    legacy = [f"Legacy Question {record.item['text']!r} at {record.path}"
              for record in matches if isinstance(record, QuestionRecord) and not _numbered_question_id(record)]
    if keys:
        message += ". Matching keys: " + ", ".join(keys)
    if legacy:
        message += ". " + ". ".join(legacy)
    if not keys and not legacy:
        message += "; name one by its text"
    raise RegistrationFailure(action, message)


@contextmanager
def select_question(production: QuestionScope, selector: str | ByQuestionKey, action: QuestionMutation,
                    identities: QuestionStaff, *, only_open: bool = False
                    ) -> Generator[OpenItemRecord | AnsweredItem]:
    """Resolve one item in the command's scope and retain its native identity."""
    own = outstanding_path(production)
    local_only = only_open or action == "followup"
    names = [production.name] if local_only else identities.names(production.name, below=True)
    paths = {name: own if name == production.name else outstanding_file(name, identities=identities) for name in names}
    if isinstance(selector, ByQuestionKey) and selector.key.startswith(new_item_dialog.KEY_PREFIX):
        scoped = new_item_dialog.read_submission_key(selector.key)
        if scoped.kind != new_item_dialog.SubmissionKind.QUESTION:
            raise RegistrationFailure(action, "This sent Question is outside the selected production.")
        lookup = cast(Callable[[new_item_dialog.ScopedSubmissionKey, staff.StaffSnapshot], AbstractContextManager[Path]],
                      import_module("scripts.review_links.review_submissions").sent_question_path)
        with lookup(scoped, identities.record) as path:
            current = read_review_question(path)
            if current.owner not in {item_path.stem for item_path in paths.values()}:
                raise RegistrationFailure(action, "This sent Question is outside the selected production.")
            yield current
        return

    records: list[OpenItemRecord] = []
    history: list[AnsweredItem] = []
    for name, path in paths.items():
        row = identities.record.by_id(staff.StaffId(path.stem))
        if path == own:
            document = question_source(production)
        elif row is not None and isinstance(row.doc, (staff.LiveDoc, staff.WrappedDoc)):
            document = row.doc.path
        else:
            document = None
        if isinstance(selector, ByQuestionKey) and selector.key.startswith("question/"):
            owner, _, identity = selector.key.removeprefix("question/").partition("/")
            if owner == path.stem and re.fullmatch(r"Q[1-9][0-9]*", identity):
                location = question_folder(document, identities) if document is not None else None
                folders = (location.path,) if isinstance(location, QuestionFolder) else ()
                found = _find_question(staff.StaffId(owner), identity, action, *folders,
                                       document=document if isinstance(document, Path) else None)
                if found is not None and found.status == "open" and "review_submission" not in found.item:
                    yield found
                    return
            continue
        records.extend(open_question_items(path, document, identities))
        if action == "answer" and not only_open and isinstance(selector, str):
            for item in answered_items(path, datetime.now(production.zone)):
                item["name"] = name
                history.append(item)

    if isinstance(selector, ByQuestionKey):
        candidates = [record for record in records if open_item_key(record) == selector.key]
        if len(candidates) != 1:
            raise RegistrationFailure(action, f"The key {selector.key!r} does not select one open item in this production.")
        yield candidates[0]
        return
    selected = resolve_question_title(selector, records, action, history=history,
                                      partial=action == "mark-important" or action == "answer" and not only_open)
    if isinstance(selected, NoTitleMatch):
        if action == "reply":
            raise RegistrationFailure(action, f"0 questions with a follow-up match {selector.strip()!r}; name one by its exact text")
        if action == "followup":
            raise RegistrationFailure(action, f"{selector}: no open question with this text in {production.name}")
        match_text = "exact text" if action == "withdraw" else "text"
        raise RegistrationFailure(action, f"0 open items match {selector.strip()!r}; name one by its {match_text}")
    yield selected


def _question_request_id(record: QuestionRecord) -> str:
    if "review_submission" in record.item:
        return new_item_dialog.read_submission(record.item["review_submission"]).request_id
    return ""


def _find_question(owner: staff.StaffId, identity: str, action: QuestionAction, *folders: Path,
                   request_id: str = "", document: Path | None = None) -> QuestionRecord | None:
    paths = _known_question_paths(owner, *folders, document=document, strict=True)
    issues = [fields for fields, number in _question_ids(owner, paths) if number == identity]
    found = [record for record in _question_records(issues, legacy_owner=owner) if _question_request_id(record) == request_id]
    if len(found) > 1:
        raise RegistrationFailure(action, f"{owner}/{identity}: more than one Question has this ID")
    return found[0] if found else None


def _current_question(record: QuestionRecord, action: QuestionAction) -> QuestionRecord:
    identity = _numbered_question_id(record)
    if not identity:
        return _current_legacy_question(record)
    current = _find_question(record.owner, identity, action, record.path.parent, request_id=_question_request_id(record))
    if current is None:
        raise RegistrationFailure(action, f"{record.owner}/{identity}: no longer open")
    return current


def current_open_question(record: QuestionRecord, action: QuestionAction) -> QuestionRecord:
    """Find the selected ID again under the owner's lock."""
    current = _current_question(record, action)
    if current.status != "open":
        raise RegistrationFailure(action, f"{record.owner}/{_numbered_question_id(current) or current.path.name}: no longer open")
    return current


def write_selected_item(selected: OpenItemRecord, production: QuestionScope, action: QuestionAction,
                        change: Callable[[OutstandingItem, datetime], None], *, close: bool = False,
                        history: Callable[[Path, OutstandingItem, datetime], None] | None = None) -> OutstandingItem:
    """Write one selected Question or reminder under its owner's lock."""
    path = state_root() / "outstanding" / f"{selected.owner}.json"
    suffix = ".answered.jsonl" if action == "answer" else ".history.jsonl"
    retained_history = (nullcontext() if history is None
                        else record_store.keep_file_on_error(path.with_suffix(suffix)))
    with outstanding_lock(path), retained_history:
        now = datetime.now(production.zone)
        if isinstance(selected, QuestionRecord):
            with _question_number_lock(selected.owner):
                current = current_open_question(selected, action)
                change(current.item, now)
                save_history = None if history is None else lambda: history(path, current.item, now)
                _ = _publish_question(replace(current, status="closed") if close else current, record_history=save_history)
            return current.item

        entries = legacy_entries(path)
        key = open_item_key(selected)
        indices = [index for index, item in enumerate(entries)
                   if "question" not in item and open_item_key(StoredReminder(selected.owner, item)) == key]
        if len(indices) != 1:
            raise RegistrationFailure(action, f"{selected.item['text']}: no longer open")
        index = indices[0]
        item = entries[index]
        change(item, now)
        if close:
            del entries[index]
        text = json.dumps(entries) + "\n"
        with record_store.keep_file_on_error(path):
            if history is not None:
                history(path, item, now)
            if path.read_text(encoding="utf-8") != text:
                write_text_atomic(path, text)
        return item


_NO_QUESTION_RELAY = AnswerRelay("", NoAnswerRelay())


def act_on_question(production: QuestionScope, selector: str | ByQuestionKey, action: QuestionMutation,
                    identities: QuestionStaff, *, words: str = "", only_open: bool = False,
                    adopt: bool = False) -> AnswerRelay:
    """Apply an action only after resolving and checking its selected item."""
    with select_question(production, selector, action, identities, only_open=only_open) as selected:
        if isinstance(selected, QuestionRecord) and "review_submission" in selected.item:
            if action == "reply":
                raise RegistrationFailure(action, "Use the scoped reply for the sent Question from Review.")
            if action not in ("answer", "mark-important"):
                raise RegistrationFailure(action, "This command cannot change a sent Question. Use Review.")
            scope = production if selected.owner == production.owner.id else question_scope(selected.owner, identities.record)
            review_sent_question(selected, scope, "read" if action == "answer" else "mark-important", "")
            report(action, "ok", f"Marked {open_item_key(selected)} {'read' if action == 'answer' else 'important'}.")
            return _NO_QUESTION_RELAY

        def adopt_state() -> None:
            if adopt and isinstance(production, Production):
                for line in adopt_moved_state(production, record=identities.record):
                    report("state-dir", "ok", line)

        if isinstance(selected, dict):
            adopt_state()
            replay_answer(production, selected, identities)
            return answer_relay(production, selected["name"], question_asker(
                selected, AskerNotRecorded("asked_by not recorded in answered record"), identities), identities)

        row = identities.record.by_id(selected.owner)
        name = row.name if row is not None else str(selected.owner)
        title = selected.item["text"]
        if action in ("reply", "followup") and not isinstance(selected, QuestionRecord):
            raise RegistrationFailure(action, f"{title}: the selected item is a reminder")
        if action == "reply":
            assert isinstance(selected, QuestionRecord)
            reply_record(selected, words, name, identities=identities)
            adopt_state()
            return _NO_QUESTION_RELAY

        def change(item: OutstandingItem, now: datetime) -> None:
            match action:
                case "answer" | "withdraw":
                    if isinstance(selected, QuestionRecord):
                        scope = production if selected.owner == production.owner.id else question_scope(selected.owner, identities.record)
                        item["asked_by"] = question_member(scope, item, identities).id
                case "mark-important":
                    mark_item_important(item, now)
                case "followup":
                    if "followup" in item:
                        raise RegistrationFailure(action, f"{title}: a follow-up already waits for the asker's reply")
                    item["followup"] = PendingFollowup(text=words.strip(), sent=now.strftime("%Y-%m-%dT%H:%M"))
                    item["waiting_why"] = review_events.require_waiting_why(words)

        def record_result(path: Path, item: OutstandingItem, now: datetime) -> None:
            if action == "answer":
                record_answer(path, name, item, now)
            else:
                record_question_history(path, [item], now, withdrawn=True)

        close = action in ("answer", "withdraw")
        updated = write_selected_item(selected, production, action, change, close=close,
                                      history=record_result if close else None)
        adopt_state()
        if action in ("answer", "followup"):
            relay = answer_relay(production, name, question_asker(updated, OpenItemWithoutAsker(), identities), identities)
            if action == "answer":
                print(relay.line)
                report(action, "ok", f"{name}: {title}")
            return relay
        report(action, "ok", title if action == "mark-important" else f"{name}: {title}")
        return _NO_QUESTION_RELAY


def mark_item_important(item: OutstandingItem, now: datetime) -> None:
    for field in ("urgency", "due", "marked"):
        _ = item.pop(field, None)
    store_ranking(item, Ranking(urgency.mark(now.replace(tzinfo=None))))


def answer(production: QuestionScope, words: str | ByQuestionKey, *, identities: QuestionStaff | None = None,
           only_open: bool = False) -> AnswerRelay:
    """Answer the selected item and resolve its asker's current staff row."""
    identities = identities if identities is not None else QuestionStaff.read()
    return act_on_question(production, words, "answer", identities, only_open=only_open)


def withdraw(production: QuestionScope, words: str | ByQuestionKey, *, identities: QuestionStaff | None = None) -> None:
    """Remove one selected item without an answer or relay."""
    identities = identities if identities is not None else QuestionStaff.read()
    _ = act_on_question(production, words, "withdraw", identities)


def followup(production: QuestionScope, text: str | ByQuestionKey, words: str, *,
             identities: QuestionStaff | None = None) -> AnswerRelay:
    """Send the asker a follow-up and record that the Question waits for a reply."""
    if not words.strip():
        raise RegistrationFailure("followup", "the follow-up is empty")
    identities = identities if identities is not None else QuestionStaff.read()
    return act_on_question(production, text, "followup", identities, words=words)


def set_waiting_why(record: QuestionRecord, text: str) -> None:
    from scripts.review_links.review_events import require_waiting_why
    reason = require_waiting_why(text)
    with outstanding_lock(state_root() / "outstanding" / (record.owner + ".json")):
        current = _question_records([Issue(record.path, frontmatter(record.path))])[0]
        if current.status != "open" or "followup" not in current.item:
            raise RegistrationFailure("waiting-why", "The item is not Waiting.")
        current.item["waiting_why"] = reason
        write_question_record(current)


def answer_followup(entry: OutstandingItem, text: str) -> None:
    """Move one pending follow-up into its answered exchanges."""
    pending = entry.pop("followup", None)
    if pending is None:
        raise RegistrationFailure("reply", f"{entry['text']}: no follow-up waits for a reply")
    entry["followups"] = [*entry.get("followups", []), FollowupExchange(followup=pending["text"], reply=text.strip())]


def record_followup_reply(entry: OutstandingItem, words: str, who: str) -> None:
    """Save the reply and its log position together."""
    events = review_events.decode(entry.get("review_log", ""))
    exchange = len(entry.get("followups", []))
    answer_followup(entry, words)
    entry.setdefault("reply_log_positions", {})[str(exchange)] = len(events)
    event = review_events.ReviewLog(datetime.now().astimezone(user_zone()), who, "Answer: " + words)
    entry["answered_at"] = event.time.isoformat()
    entry["review_log"] = review_events.encode((*events, event))


def review_sent_question(record: QuestionRecord, scope: QuestionScope,
                         action: Literal["answer", "feedback", "question", "read", "mark-important"], words: str,
                         new_reply_proof: reply_tokens.ReplyTokenProof | reply_tokens.MintReplyProof
                         = reply_tokens.MINT_REPLY_PROOF) -> None:
    """Apply the user's action to the exact sent question under its locks."""
    with outstanding_lock(outstanding_path(scope)), _question_number_lock(record.owner):
        current = current_open_question(record, "mark-important" if action == "mark-important" else "answer")
        fields = current.item.get("review_submission", {})
        if action == "read" and "followup" in current.item:
            raise RegistrationFailure(action, "The recipient still needs to reply.")
        now = datetime.now(scope.zone)
        if action == "mark-important":
            mark_item_important(current.item, now)
            write_question_record(current)
            return
        pending = current.item.pop("followup", None)
        events = review_events.decode(current.item.get("review_log", ""))
        if pending is not None:
            events = review_events.append(events, review_events.ReviewLog(now, "nate", "Superseded followup: " + pending["text"]))
        if action == "question":
            current.item["followup"] = PendingFollowup(text=words, sent=now.strftime("%Y-%m-%dT%H:%M"))
            current.item["waiting_why"] = review_events.require_waiting_why(words)
            proof = new_reply_proof if isinstance(new_reply_proof, reply_tokens.ReplyTokenProof) else reply_tokens.mint()
            current.item["review_submission"] = new_item_dialog.renewed_submission_fields(fields, proof)
        else:
            current = replace(current, status="closed")
        label = "feedback sent" if action == "feedback" else action
        event = review_events.ReviewLog(now, "nate", "Marked read" if action == "read" else label + ": " + words)
        current.item["review_log"] = review_events.encode(review_events.append(events, event))
        write_question_record(current)


def reply_to_submission(path: Path, key: new_item_dialog.ScopedSubmissionKey, words: str, token: str, *,
                        owner: staff.StaffId) -> bool:
    """Apply a reply to the current wait on a question sent by the user."""
    with (outstanding_lock(state_root() / "outstanding" / (owner + ".json")),
          _question_number_lock(owner)):
        current = read_review_question(path, owner)
        details = new_item_dialog.read_submission(current.item.get("review_submission"))
        if current.owner != owner or current.item.get("review_id") != key.item_id or details.request_id != key.request_id:
            raise new_item_dialog.SubmissionError("The selected Question changed.")
        new_item_dialog.require_reply_token(details, token)
        exchanges = current.item.get("followups", [])
        repeated = current.status == "open" and "followup" not in current.item and bool(
            exchanges and exchanges[-1]["reply"] == words)
        if not repeated and not pending_review_reply(current):
            raise new_item_dialog.SubmissionError("This item no longer accepts this reply.")
        if repeated:
            return False
        record_followup_reply(current.item, words, current.owner)
        write_question_record(current)
        return True


def reply_record(record: QuestionRecord, text: str, who: str, *, identities: QuestionStaff, question_id: str = '') -> None:
    """Answer the exact record under its owner's lock and keep the answer in its log."""
    if not text.strip():
        raise RegistrationFailure("reply", "the reply is empty")
    scope = question_scope(record.owner, identities.record)
    with outstanding_lock(outstanding_path(scope)), _question_number_lock(record.owner):
        current = current_open_question(record, "reply")
        delivery = change_routing.delivery_path(change_routing.review_root(), current.path, current.path.stem)
        if "review_submission" in current.item:
            raise RegistrationFailure("reply", "Use the scoped reply from the Review item.")
        exchange = change_routing.delivery_slots(delivery).question
        if question_id or isinstance(exchange, delivery_state.OpenQuestion):
            _ = delivery_state.question_exchange_for_reply(exchange, question_id, text.strip())
        if "followup" not in current.item:
            raise RegistrationFailure("reply", f"{current.item['text']}: no open follow-up waits for a reply")
        record_followup_reply(current.item, text, who)
        _ = _publish_question(current)
    report("reply", "ok", f"{scope.name}: {current.item['text']}: back on the open-questions list with the reply")


def reply(production: QuestionScope, words: str | ByQuestionKey, text: str, *,
          identities: QuestionStaff | None = None) -> None:
    """Record the asker's reply to the selected follow-up."""
    if not text.strip():
        raise RegistrationFailure("reply", "the reply is empty")
    identities = identities if identities is not None else QuestionStaff.read()
    _ = act_on_question(production, words, "reply", identities, words=text)


def state_block(production: Production, state: ShowrunnerState, timestamp: str) -> str:
    from scripts.production.merge_checkpoint import merged_checkpoints

    judgments = {unit.unit: unit for unit in state.units}
    unit_lines: list[str] = []
    record = staff.snapshot()
    checkpoints = {entry.unit: entry for entry in merged_checkpoints(production, record=record)}
    for row in record.find(kind="unit", parent=production.owner.id):
        name = row.name
        judgment = judgments.get(row.id)
        entry = checkpoints.get(name)
        last = f"phase {entry.phase} ({entry.commit[:7]})" if entry is not None else "none"
        phase = judgment.phase if judgment is not None else "not stated"
        wait = judgment.wait if judgment is not None else "not stated"
        unit_lines.append(f"{name}: {phase}; last merged {last}; waits on {wait}")
    held_lines = ", ".join(state.merges_held)
    open_lines = ", ".join(item.reference_text if isinstance(item, SentOutstandingQuestion) else item.text
                           for item in read_open_items(outstanding_path(production), production=production))
    return "\n".join([f"### STATE {timestamp}", *unit_lines, f"Merges accepted but held: {held_lines or 'none'}",
                      f"Open for the user: {open_lines or 'none'}", f"Next: {'; '.join(state.owed) or 'none'}", ""])


def append_log(production: Production, event: str, path: Path | None, before_compaction: bool) -> None:
    if "\n" in event or not event.strip():
        raise RegistrationFailure("log", "event must be one nonempty line")
    state = judgment_state(path, "log")
    timestamp = production_time(production, datetime.now(production.zone).isoformat())
    previous = production.log.read_text(encoding="utf-8") if production.log.exists() else ""
    count = sum(bool(re.match(r"^- \d\d:\d\d \S+: ", line)) for line in previous.split("\n")) + 1
    block = state_block(production, state, timestamp) if count % 10 == 0 or before_compaction else ""
    production.log.parent.mkdir(parents=True, exist_ok=True)
    with production.log.open("a", encoding="utf-8") as output:
        _ = output.write(f"- {timestamp}: {event}\n")
        if block:
            _ = output.write(block)
    report("log", "ok", f"event {count}" + (" with STATE" if block else ""))


def record_question_history(outstanding: Path, items: list[OutstandingItem], now: datetime, *, withdrawn: bool = False) -> None:
    """Retain two days of registrations, including questions that have closed."""
    history = outstanding.with_suffix(".history.jsonl")
    retained: list[QuestionRegistration] = []
    cutoff = now - timedelta(hours=48)
    if history.exists():
        for line in history.read_text(encoding="utf-8", newline="\n").split("\n"):
            try:
                value = cast(object, json.loads(line))
                if not isinstance(value, dict):
                    continue
                fields = cast(dict[str, object], value)
                stamp, text = fields.get("time"), fields.get("text")
                if not isinstance(stamp, str) or not isinstance(text, str):
                    continue
                dated = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                if dated.tzinfo is not None and dated >= cutoff:
                    entry = QuestionRegistration(time=stamp, text=text)
                    for field in ("question", "reviewed", "withdrawn"):
                        content = fields.get(field)
                        if isinstance(content, str):
                            if field == "question":
                                entry["question"] = content
                            elif field == "reviewed":
                                entry["reviewed"] = content
                            else:
                                entry["withdrawn"] = content
                    retained.append(entry)
            except ValueError:
                continue
    for item in items:
        entry = next((entry for entry in retained if entry["text"] == item["text"]
                      and entry.get("question") == item.get("question")), None)
        if entry is None:
            entry = QuestionRegistration(time=now.isoformat(), text=item["text"])
            if "question" in item:
                entry["question"] = item["question"]
            if "reviewed" in item:
                entry["reviewed"] = item["reviewed"]
            retained.append(entry)
        if withdrawn:
            entry["withdrawn"] = now.isoformat()
    write_text_atomic(history, "".join(json.dumps(item) + "\n" for item in retained))


def store_ranking(entry: OutstandingItem, rank: Ranking) -> None:
    """Store only what is not normal: the level, its time, and the item this one comes after."""
    for key, value in urgency.fields(rank.urgency).items():
        if key == "urgency":
            entry["urgency"] = value
        elif key == "due":
            entry["due"] = value
        else:
            entry["marked"] = value
    if rank.follows:
        entry["follows"] = rank.follows


def keep_question_fields(entry: OutstandingItem, previous: Outstanding) -> None:
    if not isinstance(previous, OutstandingReminder):
        entry["question"] = previous.question
        if previous.asked_by:
            entry["asked_by"] = previous.asked_by
    if isinstance(previous, ReviewedOutstandingQuestion):
        entry["reviewed"] = previous.reviewed
    elif isinstance(previous, ReviewUnavailableOutstandingQuestion):
        entry["review_unavailable"] = previous.reason


def keep_layout_fields(entry: OutstandingItem, stored: OutstandingItem) -> None:
    """Keep the parts and follow-ups that a showrunner's list does not carry."""
    if "parts" in stored:
        entry["parts"] = stored["parts"]
    if "followup" in stored:
        entry["followup"] = stored["followup"]
    if "followups" in stored:
        entry["followups"] = stored["followups"]
    if "reply_log_positions" in stored:
        entry["reply_log_positions"] = stored["reply_log_positions"]
    if "waiting_why" in stored:
        entry["waiting_why"] = stored["waiting_why"]
    if "review_id" in stored:
        entry["review_id"] = stored["review_id"]


class HeldRegistrationQuestion(NamedTuple):
    details: str


class ReviewedQuestion(NamedTuple):
    text: str
    reviewed: str


class ReviewUnavailableQuestion(NamedTuple):
    text: str
    question: str
    reason: str


AcceptedQuestion = ReviewedQuestion | ReviewUnavailableQuestion


def review_open_question(name: str, item: QuestionForReview) -> AcceptedQuestion | HeldRegistrationQuestion:
    # Copied registration trees can run reminder-only commands without notify installed.
    from scripts.notify import review
    outcome = review.review_question(review.QuestionKey(name, item.text), item.question)
    if isinstance(outcome, review.QuestionAccepted):
        return ReviewedQuestion(item.text, outcome.question)
    if isinstance(outcome, review.QuestionReviewUnavailable):
        return ReviewUnavailableQuestion(item.text, outcome.question, outcome.reason)
    details = outcome.questions if isinstance(outcome, review.QuestionAwaitingFacts) else outcome.details
    return HeldRegistrationQuestion("; ".join(details + tuple(f"{failure.rule}: {failure.quote}" for failure in outcome.failures)))


def questions(production: Production, path: Path | None, *, identities: QuestionStaff | None = None) -> None:
    """Record what waits on the user and review each new question before the reply asks it."""
    state = judgment_state(path, "questions")
    outstanding = outstanding_path(production)
    if isinstance(state.outstanding, ReplaceOutstanding):
        with outstanding_lock(outstanding):
            replace_outstanding(production, outstanding, state.outstanding, identities=identities)
    if isinstance(state.reports, ReplaceReports):
        reports = reports_path(production.name, identities=identities)
        write_text_atomic(reports, json.dumps(list(state.reports.items)) + "\n")
    report("questions", "ok", "recorded")


def replace_outstanding(production: Production, outstanding: Path, replacement: ReplaceOutstanding, *,
                        identities: QuestionStaff | None = None) -> None:
    """Write the showrunner's new list; a question someone filed with `ask` leaves only through `answer`."""
    identities = identities if identities is not None else QuestionStaff.read()
    records = open_question_items(outstanding, production.doc, identities)
    for item in replacement.items:
        selected = resolve_question_title(item.text, records, "questions")
        if isinstance(selected, QuestionRecord) and "review_submission" in selected.item:
            raise RegistrationFailure("questions", "This command cannot change a sent Question. Use Review.")
    previous = {record.item["text"]: parse_outstanding_entries([record.item], outstanding)[0] for record in records
                if "review_submission" not in record.item}
    stored = {entry["text"]: entry for entry in open_entries(production, identities)}
    now = datetime.now(production.zone)
    since = now.strftime("%Y-%m-%dT%H:%M")
    entries: list[OutstandingItem] = []
    pending: list[AcceptedQuestion] = []
    held: list[str] = []
    items_by_text: dict[str, OpenForUserItem] = {}
    for item in replacement.items:
        if item.text not in items_by_text:
            items_by_text[item.text] = item
    # Validate all new object questions before starting any review.
    for item in items_by_text.values():
        if item.text not in previous and isinstance(item, OpenQuestionWithoutText):
            raise RegistrationFailure("questions", f"{item.text}: a new question needs its full text in `question`")
    # Name stored title problems without blocking the replacement.
    refused: list[str] = []
    for text in items_by_text:
        checked = check_question_title(text)
        problem = checked.reason if isinstance(checked, RefusedQuestionTitle) else plumbing(text)
        if problem is not None and text in previous:
            report("questions", "reword", f"{text}: {problem}")
        elif problem is not None:
            refused.append(f"{text}: {problem}")
    if refused:
        raise RegistrationFailure("questions", "; ".join(refused))
    for item in items_by_text.values():
        entry = OutstandingItem(since=previous[item.text].since.strftime("%Y-%m-%dT%H:%M")
                                if item.text in previous else since, text=item.text)
        if item.after:
            entry["after"] = item.after
        old = previous[item.text].rank.urgency if item.text in previous else urgency.Urgency()
        store_ranking(entry, Ranking(urgency.keep_mark(item.rank.urgency, old, now.replace(tzinfo=None)),
                                     item.rank.follows))
        if item.text in previous:
            keep_question_fields(entry, previous[item.text])
            if item.text in stored:
                keep_layout_fields(entry, stored[item.text])
        elif isinstance(item, QuestionForReview):
            accepted = review_open_question(production.name, item)
            if isinstance(accepted, ReviewedQuestion):
                entry["question"], entry["reviewed"] = item.question, accepted.reviewed
                pending.append(accepted)
            elif isinstance(accepted, ReviewUnavailableQuestion):
                entry["question"], entry["review_unavailable"] = item.question, accepted.reason
                pending.append(accepted)
            else:
                held.append(f"{item.text}: {accepted.details}")
        entries.append(entry)
    if held:
        raise RegistrationFailure("questions", "question held for review: " + "; ".join(held))
    for stored in open_entries(production, identities):
        asker = stored.get("asked_by", "")
        if asker and stored["text"] not in items_by_text:
            entries.append(stored)
            report("questions", "kept", f"{stored['text']}: asked by {asker}; it leaves only through `answer` or `withdraw`")
    added = [entry for entry in entries if entry["text"] not in previous]
    with record_store.keep_file_on_error(outstanding.with_suffix(".history.jsonl")):
        write_open_entries(production, entries, identities, last=tuple(entry for entry in added if "question" in entry),
                           before_list=lambda: record_question_history(outstanding, added, now))
    for item in pending:
        accepted_text = item.reviewed if isinstance(item, ReviewedQuestion) else item.question
        print("reviewed question: " + accepted_text)


ASK_DESCRIPTION = "File a question for the user. Write its parts as ~/.claude/docs/question_template.md says."


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("prompt", "delivery"):
        delivery = commands.add_parser(action)
        _ = delivery.add_argument("--owner", required=True)
    for action in ("register", "time", "log", "questions", "open", "next", "mark-important", "ask", "answer", "withdraw",
                   "reply", "questions-table", "migrate-questions"):
        subcommand = commands.add_parser(action, description=ASK_DESCRIPTION if action == "ask" else None)
        _ = subcommand.add_argument("--production", type=str,
                                    required=action not in ("ask", "answer", "withdraw", "questions-table"))
        if action == "register":
            # Accepted and unused: a showrunner started before its name was looked up still passes it.
            _ = subcommand.add_argument("--session")
        elif action == "time":
            _ = subcommand.add_argument("stamp")
        elif action == "log":
            _ = subcommand.add_argument("event")
            _ = subcommand.add_argument("--state", type=Path)
            _ = subcommand.add_argument("--before-compaction", action="store_true")
        elif action == "questions":
            _ = subcommand.add_argument("--state", type=Path)
        elif action in ("answer", "mark-important", "withdraw", "reply"):
            target = subcommand.add_mutually_exclusive_group(required=True)
            _ = target.add_argument("--item", help="the title of the selected item")
            _ = target.add_argument("--key", help="the full key of the selected item")
            if action == "reply":
                _ = subcommand.add_argument("--text", required=True, help="your reply to the user's follow-up")
        elif action == "ask":
            _ = subcommand.add_argument("--text", default="",
                                        help="the one-line item the count and lists name, at most question_title_max_chars characters (config/production.conf); the default is --ask")
            _ = subcommand.add_argument("--ask", required=True,
                                        help="the question in one line, at most question_title_max_chars characters (config/production.conf); the parts below give the page its layout")
            _ = subcommand.add_argument("--context", default="", help="two sentences at most: why it is asked now and what waits on it")
            _ = subcommand.add_argument("--item", dest="items", action="append", default=[],
                                        help="one fact, step or command; a command in backticks; repeat it")
            _ = subcommand.add_argument("--recommend", default="",
                                        help="one choice and the reason, in one sentence; required with --choice")
            _ = subcommand.add_argument("--choice", dest="choices", action="append", default=[],
                                        help="one answer in one line that says what happens; give two or more")
            _ = subcommand.add_argument("--unit", default="", help="the asking unit's staff id; without --production, select its parent")
            _ = subcommand.add_argument("--urgency", default="", help="user, blocks, soon (with --due), normal or later")
            _ = subcommand.add_argument("--due", default="")
    args = parser.parse_args()
    action = cast(str, args.action)
    try:
        if action in ("prompt", "delivery"):
            production = read_production(cast(str, args.owner))
            schedule = update_schedule(production.doc.read_text(encoding="utf-8").splitlines())
            minutes = schedule.minutes if isinstance(schedule, ScheduledUpdates) else DEFAULT_MINUTES
            prompt = scheduled_prompt(minutes, production)
            print(f"showrunner-timer-{production.owner.name}\n{prompt}" if action == "delivery" else prompt)
            return 0
        identities = QuestionStaff.read() if action in ("ask", "reply", "answer", "withdraw", "questions-table", "mark-important") else None
        name = cast(str | None, args.production)
        if name is None:
            assert identities is not None
            if action in ("ask", "answer", "withdraw"):
                unit = cast(str, args.unit) if action == "ask" else ""
                caller = identities.record.by_current_session(staff.caller_identity())
                if not unit and caller is not None and caller.kind == "producer":
                    name = str(caller.id)
                elif unit:
                    name = unit_lookup.unit_parent(unit, session_staff.StoredReference(unit), record=identities.record).name
                elif action == "ask":
                    raise RegistrationFailure(action, "pass --unit <staff id> or --production <name>")
                else:
                    commands.choices[action].error("the following arguments are required: --production")
            else:
                name = identities.production_of(staff.caller_identity())
                if name is None:
                    raise RegistrationFailure(action, "this session is no production's showrunner; pass --production <name>")
        if action == "ask":
            assert identities is not None
            scope = question_scope(name, identities.record)
            try:
                level = urgency.read(cast(str, args.urgency), cast(str, args.due))
            except urgency.UrgencyError:
                raise RegistrationFailure("ask", "--urgency is user, blocks, soon with --due, normal or later") from None
            asked = cast(str, args.ask)
            parts = QuestionParts(ask=asked, context=cast(str, args.context), items=cast(list[str], args.items),
                                  recommend=cast(str, args.recommend), choices=cast(list[str], args.choices))
            ask(scope, cast(str, args.text) or asked, parts, cast(str, args.unit), Ranking(level),
                identities=identities)
            if isinstance(scope, Production):
                for line in adopt_moved_state(scope, record=identities.record):
                    report("state-dir", "ok", line)
            return 0
        if action in ("reply", "answer", "withdraw", "mark-important"):
            assert identities is not None
            scope = question_scope(name, identities.record)
            key = cast(str | None, args.key)
            selector = cast(str, args.item) if key is None else ByQuestionKey(key)
            words = cast(str, args.text) if action == "reply" else ""
            if action == "reply" and not words.strip():
                raise RegistrationFailure(action, "the reply is empty")
            _ = act_on_question(scope, selector, action, identities, words=words, adopt=key is None)
            return 0
        if action == "questions-table":
            assert identities is not None
            scope = question_scope(name, identities.record)
            if isinstance(scope, Production):
                for line in adopt_moved_state(scope, record=identities.record):
                    report("state-dir", "ok", line)
            questions_table(scope, identities=identities)
            return 0
        production = read_production(name, identities.record if identities else None)
        if action == "register":
            register(production)
            return 0
        if action == "questions":
            questions(production, cast(Path | None, args.state), identities=identities)
            for line in adopt_moved_state(production):
                report("state-dir", "ok", line)
            return 0
        for line in adopt_moved_state(production, record=identities.record if identities is not None else None):
            report("state-dir", "ok", line)
        if action == "time":
            report("time", "ok", production_time(production, cast(str, args.stamp)))
        elif action == "log":
            append_log(production, cast(str, args.event), cast(Path | None, args.state), cast(bool, args.before_compaction))
        elif action == "open":
            open_items(production)
        elif action == "next":
            next_item(production)
        elif action == "migrate-questions":
            migrate_questions(production)
        return 0
    except delivery_state.StaleQuestionExchange as error:
        report(action, "failed", str(error))
        return 1
    except (staff.StaffRefused, RegistrationFailure, InputError, Refusal, OSError, ValueError, json.JSONDecodeError) as error:
        step = error.step if isinstance(error, RegistrationFailure) else action
        report(step, "failed", str(error).replace("\n", "; "))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

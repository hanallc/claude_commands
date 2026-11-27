"""Read the saved questions and answers that belong on the Studio List."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from functools import partial
from pathlib import Path
from typing import Literal, cast, final
from zoneinfo import ZoneInfo

from scripts.lib.user_zone import user_zone
from scripts.production import change_routing, delivery_state, investigation, list_exchange, proposal_issue, record_store, staff, the_list, update_registration
from scripts.production.list_exchange import (
    AnswerRead as AnswerRead, Direction as Direction, ExchangeAnswer as ExchangeAnswer, Nate as Nate,
    QuestionWithoutAnswer as QuestionWithoutAnswer, RecordedAnswerer as RecordedAnswerer, Source as Source,
    TimeNotRecorded as TimeNotRecorded, ExchangeBinding, LoggedReply, QuestionEvent, QuestionOrigin,
    QuestionRecord, QuestionSlot, ReplyOrigin, TaggedReply, UnboundExchange, ReplyWithoutEvent,
)
from scripts.review_links import (
    image_store, images, list_page, new_item_dialog, question_path, question_record,
    review_events, review_items, review_submissions,
)
from scripts.review_links.review_events import ReviewLog


@dataclass(frozen=True)
class LegacyListLink:
    """The link names a number without a unique List keeper."""

    number: int


type ListLink = investigation.ListDestination | LegacyListLink


def _link_key(link: ListLink) -> tuple[Path, int] | LegacyListLink:
    return link if isinstance(link, LegacyListLink) else the_list.destination_key(link)


def _unique_links(links: Iterable[ListLink]) -> tuple[ListLink, ...]:
    by_row: dict[tuple[Path, int] | LegacyListLink, ListLink] = {}
    for link in links:
        _ = by_row.setdefault(_link_key(link), link)
    return tuple(by_row.values())


class QuestionRoute(StrEnum):
    ORDINARY = "use the ordinary linked exchange route"
    NEW = "the question can use a new List item"
    STAGED_STUDIO = "Studio must reserve a List number for this question"
    UNRESOLVED = "the question needs a verified List destination"


class StudioRouteOwner(StrEnum):
    OTHER_WRITER = "defer to Studio while it owns the filing"
    STUDIO_SEND = "finish the staged Studio filing"


@dataclass(frozen=True)
class ResolvedQuestionDestination:
    destination: investigation.ListDestination
    origin: the_list.NativeQuestionItem | the_list.NoQuestionItem


@dataclass(frozen=True)
class UnfinishedQuestionReservation:
    destination: investigation.ListDestination


@dataclass(frozen=True)
class Exchange(list_exchange.Exchange):
    list_links: tuple[ListLink, ...]
    question_route: ResolvedQuestionDestination | QuestionRoute = field(default=QuestionRoute.ORDINARY, kw_only=True)

    @property
    def question_origin(self) -> the_list.NativeQuestionItem | the_list.NoQuestionItem:
        route = self.question_route
        return route.origin if isinstance(route, ResolvedQuestionDestination) else the_list.NoQuestionItem.ORDINARY


type ReloadExchanges = Callable[[], Iterable[Exchange]]


@dataclass(frozen=True)
class KeylessQuestionMigration:
    destination: investigation.ListDestination
    row: the_list.KeylessQuestionItem
    origin: the_list.NativeQuestionItem | the_list.NoQuestionItem


@dataclass(frozen=True)
class ExchangeInventory:
    exchanges: tuple[Exchange, ...]
    list_items: frozenset[investigation.ListDestination]
    filed: tuple[tuple[investigation.ListDestination, list_exchange.FiledExchange], ...] = ()
    reload: dict[str, ReloadExchanges] = field(default_factory=dict, compare=False)
    unresolved: tuple[tuple[str, int], ...] = ()
    keyless: tuple[KeylessQuestionMigration, ...] = field(default=(), compare=False)
    question_records: dict[str, update_registration.QuestionRecord] = field(default_factory=dict, compare=False)


def _read_state(answered: datetime | TimeNotRecorded, read_times: Iterable[datetime],
                *, acknowledged: bool = False) -> AnswerRead:
    if acknowledged:
        return AnswerRead.READ
    if isinstance(answered, TimeNotRecorded):
        return AnswerRead.UNKNOWN
    if question_path.answer_is_unread(answered, read_times):
        return AnswerRead.UNREAD
    return AnswerRead.READ


def _read_times(log: Iterable[ReviewLog]) -> Iterator[datetime]:
    return (event.time for event in log
            if event.who == Nate.USER and (event.words == "read"
                or event.words.startswith(("Marked read", "Later until ", "feedback sent:", "read: "))))


def _same_prompt(first: str, second: str) -> bool:
    return first.split() == second.split()


_answerer = list_exchange.resolved_answerer

def _is_answerer(event: ReviewLog, identity: staff.StaffId, members: staff.StaffSnapshot) -> bool:
    return event.who == identity or _answerer(event.who, members) == identity


@dataclass(frozen=True)
class _Prompt:
    origin: QuestionOrigin
    identity: str
    text: str
    time: datetime


def event_digest(event: ReviewLog) -> str:
    parts = (event.time.astimezone(UTC).isoformat(), event.who, event.feedback_request_id, event.words)
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


def _reply_origin(position: int, event: ReviewLog) -> TaggedReply | LoggedReply:
    return TaggedReply(event.feedback_request_id) if event.feedback_request_id else LoggedReply(position, event_digest(event))


def _answer_event(event: ReviewLog) -> bool:
    return event.who != Nate.USER and event.words.startswith("Answer: ") and bool(event.words[8:].strip())


def _event_position(reply: ReplyOrigin, log: tuple[ReviewLog, ...]) -> int | None:
    if isinstance(reply, LoggedReply):
        if 0 <= reply.position < len(log) and event_digest(log[reply.position]) == reply.digest:
            return reply.position
        matches = [index for index, event in enumerate(log) if _answer_event(event) and event_digest(event) == reply.digest]
        if len(matches) == 1:
            return matches[0]
    elif isinstance(reply, TaggedReply):
        positions = [index for index, event in enumerate(log) if event.feedback_request_id == reply.tag
                     and _answer_event(event)]
        if len(positions) == 1:
            return positions[0]
    return None


def _native_log(item: review_items.ReviewItem, zone: ZoneInfo) -> tuple[ReviewLog, ...]:
    source = item.source
    if isinstance(source, review_items.ChangeSource):
        return review_items.dated_review_log(source.row.get("Review log", ""), zone)
    if isinstance(source, review_items.InvestigationSource):
        events = source.record.log
    elif isinstance(source, review_items.ProposalSource):
        events = proposal_issue.review_log(source.record)
    elif isinstance(source, (review_items.QuestionSource, review_items.UnownedQuestionSource)):
        return review_items.dated_review_log(source.record.item.get("review_log", ""), zone)
    else:
        events = item.log
    return tuple(replace(event, time=review_items.aware(event.time, zone)) for event in events)


def _question_prompts(log: tuple[ReviewLog, ...]) -> dict[int, _Prompt]:
    prompts: dict[int, _Prompt] = {}
    for index, event in enumerate(log):
        label, _, text = event.words.partition(": ")
        if event.who == Nate.USER and label.lower() in ("question", "followup") and text.strip():
            tag = event.feedback_request_id
            identity = "question:" + tag if tag else f"log:{index}"
            origin = QuestionSlot(tag) if tag else QuestionEvent(index)
            prompts[index] = _Prompt(origin, identity, text, event.time)
    return prompts


def _request_boundary(event: ReviewLog) -> bool:
    return _terminal_boundary(event) or (
        event.who == Nate.USER and event.words.partition(": ")[0].lower() in (
            "question", "followup", "feedback", "feedback sent"))


def _terminal_boundary(event: ReviewLog) -> bool:
    if event.who != Nate.USER:
        return False
    words = event.words.lower()
    return (words.partition(": ")[0] in ("approve", "decline", "answer", "superseded followup")
            or words.startswith("approved, assigned to ")
            or (words.startswith("user chose ") and words.endswith("; superseded the previous wait."))
            or (words.startswith("review ") and " superseded the " in words
                and words.endswith(" before the answer arrived.")))


def _first_request_boundary(log: tuple[ReviewLog, ...]) -> int:
    return next((index for index, event in enumerate(log) if _request_boundary(event)), len(log))


def _reply_windows(log: tuple[ReviewLog, ...], requests: Iterable[int]) -> dict[int, tuple[int, ...]]:
    """Find possible replies within each outgoing request's log interval."""
    windows: dict[int, list[int]] = {index: [] for index in requests}
    question = -1
    for index, event in enumerate(log):
        if index in windows:
            question = index
        elif _request_boundary(event):
            question = -1
        elif question >= 0 and _answer_event(event):
            windows[question].append(index)
    return {index: tuple(positions) for index, positions in windows.items()}


class ReplyProof(StrEnum):
    SAVED = "stored binding"
    LOCATOR = "native log position"
    TAG = "stable reply tag"
    TIME = "saved reply time"
    INTERVAL = "sole reply in a request interval"


class _SyntheticRequest(StrEnum):
    INITIAL = "native initial question"
    NATIVE_PAIR = "native question without a logged prompt"
    SLOT = "current slot without a logged prompt"
    UNLOCATED = "no unique request interval"


class _AnswererEvidence(StrEnum):
    NOT_RECORDED = "no saved author"
    RESOLVED_STAFF = "a uniquely identified staff member"


@dataclass(frozen=True)
class RequestInterval:
    text: str
    start: int | _SyntheticRequest


class NoReplyText(StrEnum):
    ABSENT = "no reply text recorded"


class NoReplyLocator(StrEnum):
    ABSENT = "no reply position recorded"


@dataclass(frozen=True)
class ReplyEvidence:
    text: str | NoReplyText = NoReplyText.ABSENT
    time: datetime | TimeNotRecorded = TimeNotRecorded.ABSENT
    answerer: staff.StaffId | RecordedAnswerer | Nate | _AnswererEvidence = _AnswererEvidence.NOT_RECORDED
    locator: int | NoReplyLocator = NoReplyLocator.ABSENT
    origin: ReplyOrigin = ReplyWithoutEvent.NATIVE_RECORD


@dataclass(frozen=True)
class VerifiedReply:
    identity: str
    position: int
    request: RequestInterval
    binding: ExchangeBinding


class UnverifiedReply(StrEnum):
    CONFLICT = "the evidence disagrees"
    MISSING = "the evidence cannot establish ownership"


@dataclass(frozen=True)
class NativePairReply:
    index: int
    position: int


@dataclass(frozen=True)
class StaffRecoveryReply:
    position: int


@dataclass(frozen=True)
class FiledReply:
    identity: str
    position: int


@dataclass(frozen=True)
class LegacyReply:
    identity: str
    start: int
    position: int


@dataclass(frozen=True)
class HistoryReply:
    start: int
    position: int


@dataclass(frozen=True)
class InitialReportReply:
    position: int


@dataclass(frozen=True)
class CurrentSlotReply:
    position: int


type ReplyCandidate = (NativePairReply | StaffRecoveryReply | FiledReply | LegacyReply | HistoryReply
                       | InitialReportReply | CurrentSlotReply)


class InferReplyProof(StrEnum):
    FROM_SOURCE = "use proof from the source"


@dataclass(frozen=True)
class RequestReplyEvidence:
    identity: str
    request: RequestInterval
    binding: ExchangeBinding
    reply: ReplyEvidence
    proof: ReplyProof
    tag: str = ""
    initial: bool = False


@dataclass(frozen=True)
class OpenQuestionSlot:
    tag: str
    text: str


@dataclass(frozen=True)
class AnsweredQuestionSlot:
    tag: str
    text: str
    answer: str
    time: datetime | TimeNotRecorded
    author: staff.StaffId | RecordedAnswerer

    @property
    def reply(self) -> ReplyEvidence:
        author = _AnswererEvidence.NOT_RECORDED if isinstance(self.author, RecordedAnswerer) else self.author
        return ReplyEvidence(self.answer, self.time, author)


class NoQuestionSlot(StrEnum):
    ABSENT = "no question slot"


class NoAcceptedSlotReply(StrEnum):
    ABSENT = "no accepted reply to the current slot"


class NoChangeListOwner(StrEnum):
    ABSENT = "no Change owner for List filing"


@dataclass(frozen=True)
class AcceptedExchanges:
    exchanges: tuple[Exchange, ...]
    verified_ids: frozenset[str]
    log_positions: dict[str, int]
    current_slot_exchange: str | NoAcceptedSlotReply
    conflicting_ids: frozenset[str]
    change_list_owner: staff.StaffId | NoChangeListOwner

    def __iter__(self) -> Iterator[Exchange]:
        return iter(self.exchanges)


@final
class ExchangeVerifier:
    """Decide whether one request owns one reply and all evidence agrees."""

    def __init__(self, source: review_items.ReviewItem | update_registration.QuestionRecord,
                 zone: ZoneInfo, members: staff.StaffSnapshot,
                 history: tuple[list_exchange.FiledExchange, ...] = ()) -> None:
        if isinstance(source, review_items.ReviewItem) and isinstance(
                source.source, (review_items.QuestionSource, review_items.UnownedQuestionSource)):
            source = source.source.record
        self.source = source
        native = isinstance(source, update_registration.QuestionRecord)
        log = (review_items.dated_review_log(source.item.get("review_log", ""), zone)
               if native else _native_log(source, zone))
        self.log = log
        self.zone = zone
        self.members = members
        self.filed = {value.exchange_id: value for value in history}
        self.history = history
        self.report = (source.source.record if not native and isinstance(source.source, review_items.InvestigationSource)
                       else None)
        self.legacy_bug = (self.report is not None and self.report.kind is investigation.InvestigationKind.BUG
                           and not self.report.belongs_on_list)
        native_pairs = source.item.get("followups", []) if native else []
        self.prompts = _question_prompts(log)
        self.windows = _reply_windows(log, self.prompts)
        self.answers = tuple(index for index, event in enumerate(log) if _answer_event(event))
        self.boundary = _first_request_boundary(log)
        self.claimed: dict[int, str] = {}
        self.verified_handoff_ids: set[str] = set()
        self.terminal = tuple(index for index, event in enumerate(log) if _terminal_boundary(event))
        self.answer_positions = frozenset(self.answers)
        self.authors = {index: _answerer(log[index].who, members) for index in self.answers}
        self.saved_tags: dict[str, set[str]] = defaultdict(set)
        self.saved_slots: dict[str, set[str]] = defaultdict(set)
        self.current_slot_exchange: str | NoAcceptedSlotReply = NoAcceptedSlotReply.ABSENT
        self.unclaimed_slot_copies: set[int] = set()
        self.disputed: set[int] = set()
        self.bound: set[int] = set()
        self.tags: dict[str, list[int]] = defaultdict(list)
        self.tagged_replies: dict[str, list[int]] = defaultdict(list)
        self.native_tags: dict[str, list[str]] = defaultdict(list)
        for index, event in enumerate(log):
            tag = event.feedback_request_id
            if tag:
                self.tags[tag].append(index)
                if index in self.answer_positions:
                    self.tagged_replies[tag.removeprefix("answer:")].append(index)
        for reply in native_pairs:
            if tag := reply.get("question_id"):
                self.native_tags[tag].append(reply["followup"])
        for saved in history:
            if not isinstance(saved.answer, ExchangeAnswer):
                continue
            binding = saved.binding
            if isinstance(binding, ExchangeBinding):
                if isinstance(binding.reply, TaggedReply):
                    self.saved_tags[binding.reply.tag].add(saved.exchange_id)
                if binding.slot_id:
                    self.saved_slots[binding.slot_id].add(saved.exchange_id)
            if saved.exchange_id.startswith("question:"):
                self.saved_slots[saved.exchange_id.removeprefix("question:")].add(saved.exchange_id)
        self.slot = self._change_slot(source if native else source.source)

    def _normalize_author(self, author: str | RecordedAnswerer) -> staff.StaffId | RecordedAnswerer:
        reference = author.name if isinstance(author, RecordedAnswerer) else author
        return _answerer(reference, self.members)

    def _source_author(self, identity: str) -> staff.StaffId | _AnswererEvidence:
        if self.report is not None:
            return self.report.owner
        if isinstance(record := self.source, update_registration.QuestionRecord):
            if "review_submission" in record.item:
                return record.owner
            if identity == "legacy-followup":
                return record.asker
        return _AnswererEvidence.RESOLVED_STAFF

    def _available(self, reply: VerifiedReply) -> bool:
        position, identity = reply.position, reply.identity
        tag = self.log[position].feedback_request_id
        return (position not in self.claimed and identity not in self.verified_handoff_ids
                and not (self.saved_tags.get(tag, set()) - {identity}))

    def _claim(self, reply: VerifiedReply) -> None:
        self.claimed[reply.position] = reply.identity
        self.verified_handoff_ids.add(reply.identity)
        if isinstance(reply.request.start, int):
            self.bound.add(reply.request.start)

    def _select(self, candidates: Iterable[ReplyCandidate]) -> VerifiedReply | UnverifiedReply:
        matches = [reply for candidate in candidates if isinstance(reply := self.verify(candidate), VerifiedReply)]
        return self._commit_unique(matches)

    def _commit_unique(self, matches: list[VerifiedReply]) -> VerifiedReply | UnverifiedReply:
        if len(matches) == 1 and self._available(matches[0]):
            reply = matches[0]
            self._claim(reply)
            return reply
        return UnverifiedReply.CONFLICT if matches else UnverifiedReply.MISSING

    def _native_request(self, text: str, tag: str, proof: ReplyProof, *,
                       initial: bool) -> RequestInterval:
        matches = [start for start, prompt in self.prompts.items() if _same_prompt(prompt.text, text)]
        tagged = [start for start in matches if tag and self.log[start].feedback_request_id == tag]
        if initial:
            start = tagged[0] if tagged == [self.boundary] else _SyntheticRequest.INITIAL
            return RequestInterval(text, start)
        if len(tagged) == 1:
            return RequestInterval(text, tagged[0])
        if len(matches) == 1 and (proof is not ReplyProof.INTERVAL or not self.log[matches[0]].feedback_request_id):
            return RequestInterval(text, matches[0])
        if proof is not ReplyProof.INTERVAL and not matches:
            return RequestInterval(text, _SyntheticRequest.NATIVE_PAIR)
        return RequestInterval(text, _SyntheticRequest.UNLOCATED)

    def _tags_agree(self, request: RequestInterval, position: int, tag: str) -> bool:
        log_tag = self.log[position].feedback_request_id
        if isinstance(request.start, int):
            request_tag = self.log[request.start].feedback_request_id
            if request_tag and tag and tag != request_tag:
                return False
            tag = tag or request_tag
        if not tag:
            return not log_tag
        if self.native_tags[tag] and (len(self.native_tags[tag]) != 1
                                     or not _same_prompt(self.native_tags[tag][0], request.text)):
            return False
        requests = [index for index in self.tags[tag] if index not in self.answer_positions]
        if requests and (requests != [request.start] or request.start not in self.prompts
                         or not _same_prompt(self.prompts[requests[0]].text, request.text)):
            return False
        answers = self.tagged_replies[tag]
        return (not log_tag or log_tag in (tag, question_path.answer_log_id(tag))) and (
            not answers or answers == [position]) and all(
                index == position for index in self.tags[question_path.answer_log_id(tag)])

    def _reply_agrees(self, position: int, evidence: ReplyEvidence) -> bool:
        event = self.log[position]
        if not isinstance(evidence.text, NoReplyText) and event.words[8:].rstrip() != evidence.text.rstrip():
            return False
        if isinstance(evidence.time, datetime) and event.time != evidence.time:
            return False
        if isinstance(evidence.origin, (LoggedReply, TaggedReply)):
            if _event_position(evidence.origin, self.log) != position:
                return False
        if isinstance(evidence.locator, int) and evidence.locator != position and (
                not isinstance(evidence.origin, LoggedReply) or evidence.locator in self.answer_positions):
            return False
        return self._author_agrees(position, evidence.answerer)

    def _author_agrees(self, position: int, author: staff.StaffId | RecordedAnswerer | Nate | _AnswererEvidence) -> bool:
        if author is _AnswererEvidence.NOT_RECORDED:
            return True
        resolved = self.authors[position]
        if author is _AnswererEvidence.RESOLVED_STAFF:
            return not isinstance(resolved, RecordedAnswerer)
        reference = author.name if isinstance(author, RecordedAnswerer) else author
        return self.log[position].who == reference or resolved == self._normalize_author(author)

    def _competing(self, start: int) -> bool:
        return any(index not in self.bound and event.who == Nate.USER
                   and event.words.partition(": ")[0].lower() in ("question", "followup", "feedback")
                   for index, event in enumerate(self.log[:start]))

    def verify(self, candidate: ReplyCandidate, proof: ReplyProof | InferReplyProof = InferReplyProof.FROM_SOURCE
               ) -> VerifiedReply | UnverifiedReply:
        position = candidate.position
        if position not in self.answer_positions or position in self.disputed:
            return UnverifiedReply.CONFLICT
        gathered = self._gather(candidate, proof)
        if isinstance(gathered, UnverifiedReply):
            return gathered
        proof = gathered.proof if isinstance(proof, InferReplyProof) else proof
        request, evidence = gathered.request, gathered.reply
        identity, tag = gathered.identity, gathered.tag
        binding = gathered.binding
        if not self._tags_agree(request, position, tag) or not self._reply_agrees(position, evidence):
            return UnverifiedReply.CONFLICT
        owner = self._source_author(identity)
        if not self._author_agrees(position, owner):
            return UnverifiedReply.CONFLICT
        start, event = request.start, self.log[position]
        tagged = bool(event.feedback_request_id)
        beginning = start if isinstance(start, int) else -1
        if any(beginning < index < position for index in self.terminal):
            return UnverifiedReply.CONFLICT
        if isinstance(start, int):
            if position <= start or (position not in self.windows[start] and (gathered.initial or not tagged)):
                return UnverifiedReply.CONFLICT
        elif start in (_SyntheticRequest.INITIAL, _SyntheticRequest.NATIVE_PAIR):
            if position >= self.boundary:
                return UnverifiedReply.CONFLICT
        elif start is _SyntheticRequest.UNLOCATED or not tagged:
            return UnverifiedReply.MISSING

        slot = self.slot
        if isinstance(start, int):
            tag = tag or self.log[start].feedback_request_id
        stable_slot = binding.slot_id
        if self.saved_slots.get(tag or stable_slot, set()) - {identity}:
            return UnverifiedReply.CONFLICT
        current_slot = not isinstance(slot, NoQuestionSlot) and (slot.tag in (tag, stable_slot)
            or (not isinstance(candidate, FiledReply) and not tag and _same_prompt(request.text, slot.text)))
        if current_slot:
            assert not isinstance(slot, NoQuestionSlot)
            if (isinstance(slot, OpenQuestionSlot) or not _same_prompt(request.text, slot.text)
                    or not self._reply_agrees(position, slot.reply)):
                return UnverifiedReply.CONFLICT
            if not tag and not tagged:
                if not isinstance(slot.time, datetime) and not isinstance(evidence.time, datetime):
                    return UnverifiedReply.MISSING
                if sum(_same_prompt(prompt.text, slot.text) for prompt in self.prompts.values()) != 1:
                    return UnverifiedReply.MISSING

        outcome = VerifiedReply(identity, position, request, binding)
        if not self._available(outcome):
            return UnverifiedReply.CONFLICT

        if proof is ReplyProof.SAVED and not isinstance(evidence.origin, (LoggedReply, TaggedReply)):
            return UnverifiedReply.MISSING
        if proof is ReplyProof.LOCATOR and isinstance(evidence.locator, NoReplyLocator):
            return UnverifiedReply.MISSING
        if proof is ReplyProof.TAG and not tagged:
            return UnverifiedReply.MISSING
        if proof is ReplyProof.TIME and not isinstance(evidence.time, datetime):
            return UnverifiedReply.MISSING
        if proof is ReplyProof.INTERVAL:
            if start is _SyntheticRequest.INITIAL:
                if sum(index < self.boundary for index in self.answers) != 1:
                    return UnverifiedReply.MISSING
            elif isinstance(start, int):
                if len(self.windows[start]) != 1 or self._competing(start):
                    return UnverifiedReply.MISSING
            else:
                return UnverifiedReply.MISSING
        return outcome

    def _gather(self, candidate: ReplyCandidate, proof: ReplyProof | InferReplyProof
                ) -> RequestReplyEvidence | UnverifiedReply:
        match candidate:
            case NativePairReply():
                return self._native_evidence(candidate, proof)
            case StaffRecoveryReply():
                return self._staff_recovery_evidence(candidate)
            case FiledReply():
                return self._filed_evidence(candidate)
            case LegacyReply():
                return self._legacy_evidence(candidate)
            case HistoryReply():
                return self._history_evidence(candidate)
            case InitialReportReply():
                return self._initial_report_evidence(candidate)
            case CurrentSlotReply():
                return self._slot_reply_evidence(candidate)

    def _native_evidence(self, candidate: NativePairReply, proof: ReplyProof | InferReplyProof
                         ) -> RequestReplyEvidence | UnverifiedReply:
        record = self.source
        if not isinstance(record, update_registration.QuestionRecord):
            return UnverifiedReply.MISSING
        fields = record.item
        replies = fields.get("followups", [])
        index = candidate.index
        if not 0 <= index < len(replies):
            return UnverifiedReply.MISSING
        pair = replies[index]
        tag, text = pair.get("question_id", ""), pair["followup"]
        identity = f"followup:{index + 1}"
        saved = self.filed.get(identity)
        binding = ExchangeBinding(QuestionRecord(identity), _reply_origin(candidate.position, self.log[candidate.position]))
        saved_binding = saved.binding if saved is not None else UnboundExchange.LEGACY
        origin = saved_binding.reply if isinstance(saved_binding, ExchangeBinding) else ReplyWithoutEvent.NATIVE_RECORD
        stamp = pair.get("replied_at") or (fields.get("answered_at") if index == len(replies) - 1 else None)
        time = review_items.moment(stamp, self.zone) if stamp is not None else TimeNotRecorded.ABSENT
        initial = (index == 0 and "review_submission" in fields and _same_prompt(fields.get("question", fields["text"]), text))
        proof = ReplyProof.INTERVAL if isinstance(proof, InferReplyProof) else proof
        request = self._native_request(text, tag, proof, initial=initial)
        author: staff.StaffId | RecordedAnswerer | Nate | _AnswererEvidence = _AnswererEvidence.NOT_RECORDED
        if saved is not None and isinstance(saved_binding, ExchangeBinding) and isinstance(origin, (LoggedReply, TaggedReply)):
            if saved.question != text or not isinstance(saved.answer, ExchangeAnswer) or saved.answer.text != pair["reply"]:
                return UnverifiedReply.CONFLICT
            binding, author = saved_binding, saved.answerer
            if isinstance(saved.answer.time, datetime):
                if isinstance(time, datetime) and time != saved.answer.time:
                    return UnverifiedReply.CONFLICT
                time = saved.answer.time
        locators = fields.get("reply_log_positions", {})
        evidence = ReplyEvidence(pair["reply"], time, author, locators.get(str(index), NoReplyLocator.ABSENT), origin)
        return RequestReplyEvidence(identity, request, binding, evidence, proof, tag, initial)

    def _staff_recovery_evidence(self, candidate: StaffRecoveryReply) -> RequestReplyEvidence | UnverifiedReply:
        record = self.source
        if not isinstance(record, update_registration.QuestionRecord) or len(self.prompts) != 1:
            return UnverifiedReply.MISSING
        start, prompt = next(iter(self.prompts.items()))
        if not _same_prompt(prompt.text, record.item.get("waiting_why", "")):
            return UnverifiedReply.CONFLICT
        identity = "legacy-followup"
        saved = self.filed.get(identity)
        binding = ExchangeBinding(prompt.origin, _reply_origin(candidate.position, self.log[candidate.position]))
        evidence = ReplyEvidence()
        if saved is not None:
            answer = saved.answer
            if (saved.question != prompt.text or saved.asked_at != prompt.time or saved.answerer != record.asker
                    or not isinstance(answer, ExchangeAnswer)):
                return UnverifiedReply.CONFLICT
            if isinstance(saved.binding, ExchangeBinding):
                evidence = ReplyEvidence(answer.text, answer.time, saved.answerer, origin=saved.binding.reply)
            else:
                evidence = ReplyEvidence(answer.text, answer.time, saved.answerer)
        proof = ReplyProof.SAVED if isinstance(evidence.origin, (LoggedReply, TaggedReply)) else ReplyProof.INTERVAL
        return RequestReplyEvidence(identity, RequestInterval(prompt.text, start), binding, evidence, proof)

    def _filed_evidence(self, candidate: FiledReply) -> RequestReplyEvidence | UnverifiedReply:
        saved = self.filed.get(candidate.identity)
        if saved is None or not isinstance(saved.binding, ExchangeBinding) or not isinstance(saved.answer, ExchangeAnswer):
            return UnverifiedReply.MISSING
        binding, answer = saved.binding, saved.answer
        origin = binding.question
        if isinstance(origin, QuestionEvent):
            prompt = _bound_event_prompt(origin, saved, candidate.position, self.prompts, self.windows)
            if prompt is None or not isinstance(prompt.origin, QuestionEvent):
                return UnverifiedReply.CONFLICT
            start = prompt.origin.position
            tag = ""
        elif isinstance(origin, QuestionSlot):
            starts = [index for index, prompt in self.prompts.items() if prompt.origin == origin]
            if len(starts) > 1:
                return UnverifiedReply.CONFLICT
            start = starts[0] if starts else _SyntheticRequest.SLOT
            tag = origin.identity
        else:
            return UnverifiedReply.MISSING
        evidence = ReplyEvidence(answer.text, answer.time, saved.answerer, origin=binding.reply)
        request = RequestInterval(saved.question, start)
        return RequestReplyEvidence(saved.exchange_id, request, binding, evidence, ReplyProof.SAVED, tag)

    def _legacy_evidence(self, candidate: LegacyReply) -> RequestReplyEvidence | UnverifiedReply:
        saved = self.filed.get(candidate.identity)
        prompt = self.prompts.get(candidate.start)
        if saved is None or prompt is None or not isinstance(saved.answer, ExchangeAnswer):
            return UnverifiedReply.MISSING
        answer = saved.answer
        old_slot = saved.exchange_id.removeprefix("question:") if saved.exchange_id.startswith("question:") else ""
        if prompt.text != saved.question:
            return UnverifiedReply.CONFLICT
        tag = self.log[candidate.start].feedback_request_id
        if isinstance(prompt.origin, QuestionSlot):
            if old_slot and old_slot != tag:
                return UnverifiedReply.CONFLICT
        elif old_slot:
            prompt = replace(prompt, origin=QuestionEvent(candidate.start, old_slot))
        elif not isinstance(saved.asked_at, datetime) or prompt.time != saved.asked_at:
            return UnverifiedReply.CONFLICT
        if not isinstance(answer.time, datetime) and (not tag or not old_slot):
            return UnverifiedReply.MISSING
        evidence = ReplyEvidence(answer.text, answer.time, saved.answerer)
        proof = ReplyProof.TAG if self.tagged_replies.get(tag) else ReplyProof.INTERVAL
        binding = ExchangeBinding(prompt.origin, _reply_origin(candidate.position, self.log[candidate.position]))
        request = RequestInterval(prompt.text, candidate.start)
        return RequestReplyEvidence(saved.exchange_id, request, binding, evidence, proof, tag)

    def _history_evidence(self, candidate: HistoryReply) -> RequestReplyEvidence | UnverifiedReply:
        prompt = self.prompts.get(candidate.start)
        if prompt is None:
            return UnverifiedReply.MISSING
        tag = self.log[candidate.start].feedback_request_id
        slot = self.slot
        time = slot.time if isinstance(slot, AnsweredQuestionSlot) and tag == slot.tag else TimeNotRecorded.ABSENT
        proof = ReplyProof.TAG if self.tagged_replies.get(tag) else ReplyProof.TIME if isinstance(time, datetime) else ReplyProof.INTERVAL
        event = self.log[candidate.position]
        identity = _new_event_id(prompt, event)
        if identity in self.filed or any(
                saved.binding is UnboundExchange.LEGACY and saved.exchange_id != "initial"
                and saved.exchange_id not in self.verified_handoff_ids and saved.question == prompt.text
                and isinstance(saved.answer, ExchangeAnswer) and saved.answer.text == event.words[8:]
                and (not isinstance(saved.answer.time, datetime) or saved.answer.time == event.time)
                for saved in self.history):
            return UnverifiedReply.CONFLICT
        if not isinstance(slot, NoQuestionSlot) and not tag and _same_prompt(slot.text, prompt.text):
            prompt = replace(prompt, origin=QuestionEvent(candidate.start, slot.tag))
        binding = ExchangeBinding(prompt.origin, _reply_origin(candidate.position, event))
        request = RequestInterval(prompt.text, candidate.start)
        return RequestReplyEvidence(identity, request, binding, ReplyEvidence(time=time), proof, tag)

    def _initial_report_evidence(self, candidate: InitialReportReply) -> RequestReplyEvidence | UnverifiedReply:
        record = self.report
        if record is None:
            return UnverifiedReply.MISSING
        position = candidate.position
        event = self.log[position]
        if record.answer and event.words[8:] != record.answer and not self._later_report_answer_is_claimed():
            return UnverifiedReply.CONFLICT
        saved = self.filed.get("initial")
        binding = ExchangeBinding(QuestionRecord("initial"), _reply_origin(position, event))
        evidence = ReplyEvidence()
        if saved is not None:
            if (saved.question != record.question or not isinstance(saved.answer, ExchangeAnswer)
                    or not self._author_agrees(position, saved.answerer)):
                return UnverifiedReply.CONFLICT
            if isinstance(saved.binding, ExchangeBinding):
                binding = saved.binding
                evidence = ReplyEvidence(saved.answer.text, saved.answer.time, saved.answerer, origin=binding.reply)
            else:
                evidence = ReplyEvidence(saved.answer.text, saved.answer.time, saved.answerer)
        proof = ReplyProof.SAVED if isinstance(evidence.origin, (TaggedReply, LoggedReply)) else ReplyProof.INTERVAL
        request = RequestInterval(record.question, _SyntheticRequest.INITIAL)
        return RequestReplyEvidence("initial", request, binding, evidence, proof, initial=True)

    def _later_report_answer_is_claimed(self) -> bool:
        if self.report is None or not self.answers:
            return False
        last = self.answers[-1]
        return (last >= self.boundary and last in self.claimed
                and self.log[last].words == "Answer: " + self.report.answer)

    def _slot_reply_evidence(self, candidate: CurrentSlotReply) -> RequestReplyEvidence | UnverifiedReply:
        slot = self.slot
        if not isinstance(slot, AnsweredQuestionSlot):
            return UnverifiedReply.CONFLICT if isinstance(slot, OpenQuestionSlot) else UnverifiedReply.MISSING
        starts = [index for index, prompt in self.prompts.items() if prompt.origin == QuestionSlot(slot.tag)]
        if len(starts) > 1:
            return UnverifiedReply.CONFLICT
        start = starts[0] if starts else _SyntheticRequest.SLOT
        binding = ExchangeBinding(QuestionSlot(slot.tag), _reply_origin(candidate.position, self.log[candidate.position]))
        request = RequestInterval(slot.text, start)
        return RequestReplyEvidence(f"question:{slot.tag}", request, binding, slot.reply, ReplyProof.TAG, slot.tag)

    def _native_exchanges(self, key: str, links: tuple[ListLink, ...],
                         unresolved: list[tuple[str, int]] | None) -> Iterator[Exchange]:
        record = self.source
        assert isinstance(record, update_registration.QuestionRecord)
        fields = record.item
        zone, members, log = self.zone, self.members, self.log
        source = Source.SCOPED_REPLY if "review_submission" in fields else Source.STAFF_QUESTION
        initial = fields.get("question", fields["text"])
        filed = review_items.moment(fields["since"], zone)
        closed = record.status == "closed"
        if source is Source.STAFF_QUESTION:
            answered = next((event for event in reversed(log)
                             if event.who == Nate.USER and event.words.startswith("answer: ")), None)
            answer = (ExchangeAnswer(answered.words.removeprefix("answer: "), answered.time, AnswerRead.NOT_TRACKED)
                      if answered is not None else QuestionWithoutAnswer.CLOSED if closed else QuestionWithoutAnswer.OPEN)
            self.verified_handoff_ids.add("initial")
            yield Exchange(source, key, "initial", Direction.QUESTION_TO_NATE, initial, filed, record.asker,
                           Nate.USER, answer, links, binding=ExchangeBinding(QuestionRecord("initial"),
                               ReplyWithoutEvent.NATIVE_RECORD if isinstance(answer, ExchangeAnswer) else ReplyWithoutEvent.NO_ANSWER))
        replies = fields.get("followups", [])
        positions = self._native_positions()
        derived = (self._staff_log_reply()
                   if source is Source.STAFF_QUESTION and closed and not replies else None)
        if derived is not None:
            prompt, verified = derived
            event = log[verified.position]
            answer = ExchangeAnswer(event.words[8:], event.time, AnswerRead.READ)
            yield Exchange(source, key, "legacy-followup", Direction.ANSWER_TO_NATE, prompt.text,
                           prompt.time, Nate.USER, record.asker, answer, links,
                           binding=verified.binding)
        if unresolved is not None:
            claimed = {verified.position for verified in positions.values()}
            if derived is not None:
                claimed.add(derived[1].position)
            if count := sum(index not in claimed and _answer_event(event) for index, event in enumerate(log)):
                unresolved.append((key, count))
        read_times = tuple(_read_times(log))
        for index, reply in enumerate(replies):
            verified = positions.get(index)
            if verified is None and (saved := self._retained_native_pair(index)) is not None:
                yield _saved_scoped_exchange(saved, links, read_times, closed)
                continue
            position = verified.position if verified is not None else None
            stamp = reply.get("replied_at")
            if stamp is None and position is None and index == len(replies) - 1:
                stamp = fields.get("answered_at")
            answered_at = (log[position].time if position is not None else review_items.moment(stamp, zone)
                           if stamp is not None else TimeNotRecorded.ABSENT)
            start = verified.request.start if verified is not None else _SyntheticRequest.UNLOCATED
            asked_at = (log[start].time if isinstance(start, int) else filed
                        if source is Source.SCOPED_REPLY and index == 0 and _same_prompt(reply["followup"], initial)
                        else TimeNotRecorded.ABSENT)
            answer = ExchangeAnswer(reply["reply"], answered_at, _read_state(answered_at, read_times, acknowledged=closed))
            event = log[position] if position is not None else None
            author = _answerer(event.who, members) if event is not None else record.owner
            identity = f"followup:{index + 1}"
            binding = verified.binding if verified is not None else ExchangeBinding(
                QuestionRecord(identity), ReplyWithoutEvent.NATIVE_RECORD)
            self.verified_handoff_ids.add(identity)
            yield Exchange(source, key, identity, Direction.ANSWER_TO_NATE, reply["followup"],
                           asked_at, Nate.USER, author, answer, links, binding=binding)
        if source is Source.SCOPED_REPLY and not closed and (pending := fields.get("followup")) is not None:
            identity = f"followup:{len(replies) + 1}"
            self.verified_handoff_ids.add(identity)
            yield Exchange(source, key, identity, Direction.ANSWER_TO_NATE, pending["text"],
                           review_items.moment(pending["sent"], zone), Nate.USER, record.owner,
                           QuestionWithoutAnswer.OPEN, links,
                           binding=ExchangeBinding(QuestionRecord(identity), ReplyWithoutEvent.NO_ANSWER))

    def _native_positions(self) -> dict[int, VerifiedReply]:
        record = self.source
        assert isinstance(record, update_registration.QuestionRecord)
        fields = record.item
        replies = fields.get("followups", [])
        locators = fields.get("reply_log_positions", {})
        positions: dict[int, VerifiedReply] = {}

        for strength in (ReplyProof.SAVED, ReplyProof.LOCATOR, ReplyProof.TAG, ReplyProof.TIME):
            candidates: dict[int, list[tuple[int, VerifiedReply]]] = defaultdict(list)
            for index in range(len(replies)):
                if index in positions:
                    continue
                saved = self.filed.get(f"followup:{index + 1}")
                binding = saved.binding if saved is not None else UnboundExchange.LEGACY
                if strength is ReplyProof.SAVED:
                    located = _event_position(binding.reply, self.log) if isinstance(binding, ExchangeBinding) else None
                    events = () if located is None else (located,)
                elif strength is ReplyProof.LOCATOR:
                    located = locators.get(str(index))
                    events = () if located is None else (located,)
                elif strength is ReplyProof.TAG:
                    events = self.tagged_replies.get(replies[index].get("question_id", ""), ())
                else:
                    events = self.answers
                for position in events:
                    if isinstance(verified := self.verify(NativePairReply(index, position), strength), VerifiedReply):
                        candidates[position].append((index, verified))
            totals: dict[int, int] = defaultdict(int)
            for claims in candidates.values():
                for index, _ in claims:
                    totals[index] += 1
            for position, claims in sorted(candidates.items()):
                if len(claims) != 1:
                    self.disputed.add(position)
                    continue
                index, verified = claims[0]
                if totals[index] != 1:
                    self.disputed.add(position)
                    continue
                if isinstance(self._commit_unique([verified]), VerifiedReply):
                    positions[index] = verified

        for position in self.answers:
            matches = [(index, verified) for index in range(len(replies)) if index not in positions
                       and isinstance(verified := self.verify(NativePairReply(index, position), ReplyProof.INTERVAL), VerifiedReply)]
            if len(matches) == 1:
                index, verified = matches[0]
                if all((other < index) == (known.position < position) for other, known in positions.items()):
                    if isinstance(self._commit_unique([verified]), VerifiedReply):
                        positions[index] = verified
        return positions

    def _retained_native_pair(self, index: int) -> list_exchange.FiledExchange | None:
        record = self.source
        assert isinstance(record, update_registration.QuestionRecord)
        saved = self.filed.get(f"followup:{index + 1}")
        pair = record.item.get("followups", [])[index]
        if (saved is not None and isinstance(saved.answer, ExchangeAnswer)
                and isinstance(saved.binding, ExchangeBinding) and isinstance(saved.binding.reply, (LoggedReply, TaggedReply))
                and saved.question == pair["followup"] and saved.answer.text == pair["reply"]):
            return saved
        return None

    def _staff_log_reply(self) -> tuple[_Prompt, VerifiedReply] | None:
        if len(self.prompts) != 1:
            return None
        start, prompt = next(iter(self.prompts.items()))
        reply = self._select(StaffRecoveryReply(position) for position in self.windows[start])
        return (prompt, reply) if isinstance(reply, VerifiedReply) else None

    def _legacy_matches(self, saved: list_exchange.FiledExchange) -> Iterator[tuple[_Prompt, VerifiedReply]]:
        for start, prompt in self.prompts.items():
            tag = self.log[start].feedback_request_id
            for position in self.tagged_replies.get(tag) or self.windows[start]:
                if isinstance(verified := self.verify(LegacyReply(saved.exchange_id, start, position)), VerifiedReply):
                    yield prompt, verified

    @staticmethod
    def _verify_native_initial(record: investigation.Investigation, time: datetime,
                               saved: list_exchange.FiledExchange | None) -> ExchangeBinding | UnverifiedReply:
        origin = ExchangeBinding(QuestionRecord("initial"), ReplyWithoutEvent.NATIVE_RECORD)
        if saved is None:
            return origin
        binding, answer = saved.binding, saved.answer
        if (saved.question != record.question or saved.answerer != record.owner
                or not isinstance(answer, ExchangeAnswer) or answer.text != record.answer
                or isinstance(answer.time, datetime) and answer.time != time
                or isinstance(binding, ExchangeBinding) and binding.reply is not ReplyWithoutEvent.NATIVE_RECORD):
            return UnverifiedReply.CONFLICT
        return binding if isinstance(binding, ExchangeBinding) else origin

    def _scoped_exchanges(self, key: str, links: tuple[ListLink, ...],
                         unresolved: list[tuple[str, int]] | None) -> Iterator[Exchange]:
        """Project accepted exchanges and retain the verified claims for Review."""
        item = self.source
        assert isinstance(item, review_items.ReviewItem)
        source = item.source
        log, zone = self.log, self.zone
        slot = self.slot
        history = self.history
        read_times = tuple(_read_times(log))
        acknowledged = item.state is review_items.ReviewState.DONE
        record = self.report
        if record is not None and record.read:
            read_times += (review_items.moment(record.read, zone),)
        legacy_bug = self.legacy_bug

        def exchange(prompt: _Prompt, author: staff.StaffId | RecordedAnswerer, answer: ExchangeAnswer,
                     binding: ExchangeBinding) -> Exchange:
            return Exchange(Source.SCOPED_REPLY, key, prompt.identity, Direction.ANSWER_TO_NATE,
                            prompt.text, prompt.time, Nate.USER, author, answer, links, binding=binding)

        def from_event(prompt: _Prompt, verified: VerifiedReply) -> Exchange:
            event = log[verified.position]
            state = _read_state(event.time, read_times, acknowledged=acknowledged)
            return exchange(replace(prompt, identity=verified.identity), self.authors[verified.position],
                            ExchangeAnswer(event.words[8:], event.time, state), verified.binding)

        accepted: dict[str, Exchange] = {}
        bound_slots: set[str] = set()

        def keep(value: Exchange) -> None:
            accepted[value.exchange_id] = value
            if isinstance(value.binding, ExchangeBinding) and value.binding.slot_id:
                bound_slots.add(value.binding.slot_id)

        legacy = [saved for saved in history if saved.binding is UnboundExchange.LEGACY and saved.exchange_id != "initial"]
        saved_initial = self.filed.get("initial")
        prior_claims: dict[int, list[VerifiedReply]] = defaultdict(list)
        for saved in history:
            if saved in legacy or saved is saved_initial:
                continue
            keep(_saved_scoped_exchange(saved, links, read_times, acknowledged))
            if isinstance(saved.binding, ExchangeBinding):
                position = _event_position(saved.binding.reply, log)
                if position is not None:
                    verified = self.verify(FiledReply(saved.exchange_id, position))
                    if isinstance(verified, VerifiedReply):
                        prior_claims[position].append(verified)
        for position, claims in prior_claims.items():
            if position not in self.disputed and isinstance(self._commit_unique(claims), UnverifiedReply):
                self.disputed.add(position)

        upgrades: dict[int, list[tuple[_Prompt, VerifiedReply]]] = defaultdict(list)
        for saved in legacy:
            matches = tuple(self._legacy_matches(saved))
            if len(matches) == 1:
                prompt, match = matches[0]
                upgrades[match.position].append((prompt, match))
        upgraded: set[str] = set()
        for position, matches in upgrades.items():
            match = self._commit_unique([reply for _, reply in matches])
            if isinstance(match, UnverifiedReply):
                self.disputed.add(position)
                continue
            keep(from_event(matches[0][0], match))
            upgraded.add(match.identity)
        for saved in legacy:
            if saved.exchange_id not in upgraded:
                keep(_saved_scoped_exchange(saved, links, read_times, acknowledged))

        new_pairs: dict[int, Exchange] = {}
        for start, prompt in self.prompts.items():
            if start in self.bound:
                continue
            tag = self.log[start].feedback_request_id
            if tag in bound_slots or (not isinstance(slot, NoQuestionSlot) and not tag and slot.tag in bound_slots
                                      and _same_prompt(slot.text, prompt.text)):
                continue
            tagged = self.tagged_replies.get(tag, ())
            match = self._select(HistoryReply(start, position) for position in tagged or self.windows[start])
            if isinstance(match, VerifiedReply):
                new_pairs[match.position] = from_event(prompt, match)

        initial = None
        suspect = saved_initial.answer if saved_initial is not None and isinstance(saved_initial.answer, ExchangeAnswer) else None
        if record is not None and (legacy_bug or (record.kind is investigation.InvestigationKind.INVESTIGATION
                and record.purpose in (investigation.InvestigationPurpose.INVESTIGATE,
                                       investigation.InvestigationPurpose.LIST_INVESTIGATION))):
            prompt = _Prompt(QuestionRecord("initial"), "initial", record.question, item.times.filed)
            match = self._select(InitialReportReply(position) for position in self.answers if position < self.boundary)
            if isinstance(match, VerifiedReply):
                initial = from_event(prompt, match)
            if (initial is None and not legacy_bug and not self.answers and self.boundary == len(log)
                    and record.answer and record.answered):
                time = review_items.moment(record.answered, zone)
                binding = self._verify_native_initial(record, time, saved_initial)
                if isinstance(binding, ExchangeBinding):
                    state = _read_state(time, read_times, acknowledged=acknowledged)
                    initial = exchange(prompt, record.owner, ExchangeAnswer(record.answer, time, state), binding)
            if initial is not None:
                keep(initial)
                self.verified_handoff_ids.add("initial")
                suspect = None

        for position, value in new_pairs.items():
            if (legacy_bug and initial is None) or (suspect is not None and log[position].words == "Answer: " + suspect.text):
                _ = self.claimed.pop(position, None)
                self.verified_handoff_ids.discard(value.exchange_id)
            else:
                keep(value)

        if isinstance(slot, AnsweredQuestionSlot):
            self._accept_slot(accepted, bound_slots, slot, key, links, read_times, acknowledged)

        if unresolved is not None:
            possible = {position for positions in self.windows.values() for position in positions}
            for start in self.prompts:
                possible.update(self.tagged_replies.get(log[start].feedback_request_id, ()))
            if record is not None:
                possible.update(self.answers)
            if not isinstance(slot, NoQuestionSlot):
                possible.update(self.tagged_replies.get(slot.tag, ()))
            first_unbound = min(set(self.prompts) - self.bound, default=len(log))
            possible.update(index for index in self.answers if index > first_unbound and not log[index].feedback_request_id)
            if isinstance(source, review_items.ChangeSource) and isinstance(feedback_slot := source.slots.question,
                                                                           review_items.CompletedFeedback):
                feedback = _completed_feedback_reply(source, feedback_slot, log, self.prompts, self.members)
                if feedback is not None:
                    possible.discard(feedback)
            possible.update(self.unclaimed_slot_copies)
            count = len(possible - self.claimed.keys())
            if count or suspect is not None:
                unresolved.append((key, count or 1))
        yield from accepted.values()

    def _accept_slot(self, accepted: dict[str, Exchange], bound_slots: set[str], slot: AnsweredQuestionSlot,
                     key: str, links: tuple[ListLink, ...], read_times: tuple[datetime, ...], acknowledged: bool) -> None:
        if slot.tag not in bound_slots:
            item = self.source
            assert isinstance(item, review_items.ReviewItem)
            prompt = _slot_prompt(slot, self.prompts, item.times.filed)
            if self.saved_slots.get(slot.tag, set()) - {prompt.identity}:
                return
            reply = self._select(CurrentSlotReply(position) for position in self.tagged_replies.get(slot.tag, ()))
            if isinstance(reply, VerifiedReply):
                position = reply.position
                event = self.log[position]
                binding, time, author, text = reply.binding, event.time, self.authors[position], event.words[8:]
            else:
                binding = ExchangeBinding(prompt.origin, ReplyWithoutEvent.SLOT)
                time, author, text = slot.time, slot.author, slot.answer
            answer = ExchangeAnswer(text, time, _read_state(time, read_times, acknowledged=acknowledged))
            accepted[prompt.identity] = Exchange(Source.SCOPED_REPLY, key, prompt.identity, Direction.ANSWER_TO_NATE,
                                                 slot.text, prompt.time, Nate.USER, author, answer, links, binding=binding)
        for exchange in accepted.values():
            binding, answer = exchange.binding, exchange.answer
            if (isinstance(binding, ExchangeBinding) and binding.slot_id == slot.tag
                    and exchange.question == slot.text and isinstance(answer, ExchangeAnswer)
                    and answer.text == slot.answer
                    and (not isinstance(slot.time, datetime) or answer.time == slot.time)
                    and (isinstance(slot.author, RecordedAnswerer)
                         or self._normalize_author(exchange.answerer) == slot.author)
                    and (binding.reply is ReplyWithoutEvent.SLOT or exchange.exchange_id in self.verified_handoff_ids)):
                self.current_slot_exchange = exchange.exchange_id
                self.verified_handoff_ids.add(exchange.exchange_id)
                if isinstance(exchange.answerer, RecordedAnswerer) and isinstance(answer.time, datetime):
                    self.unclaimed_slot_copies = {
                        position for position in self.answers if position not in self.claimed
                        and not self.log[position].feedback_request_id
                        and self.log[position].time == answer.time
                        and self.log[position].words[8:].rstrip() == answer.text.rstrip()
                    }
                return

    def resolve(self, key: str, links: tuple[ListLink, ...],
                unresolved: list[tuple[str, int]] | None = None) -> AcceptedExchanges:
        """Reject conflicting claims before filing or confirming a handoff."""
        pending: list[tuple[str, int]] = []
        native = isinstance(self.source, update_registration.QuestionRecord)
        candidates = tuple(self._native_exchanges(key, links, pending) if native
                           else self._scoped_exchanges(key, links, pending))
        conflicts = set(list_exchange.conflicting_reply_ids((*self.history, *candidates), self.members))
        if self.unclaimed_slot_copies:
            conflicts.add(self.current_slot_exchange)
        positions = {identity: position for position, identity in self.claimed.items() if identity not in conflicts}
        if self.report is not None and (initial := positions.get("initial")) is not None:
            if (self.report.answer and self.log[initial].words != "Answer: " + self.report.answer
                    and self.answers[-1] not in positions.values()):
                conflicts.add("initial")
                _ = positions.pop("initial")
        if unresolved is not None:
            count = sum(count for _, count in pending) + len(conflicts)
            if count:
                unresolved.append((key, count))
        return AcceptedExchanges(tuple(value for value in candidates if value.exchange_id not in conflicts),
                                 frozenset(self.verified_handoff_ids - conflicts), positions,
                                 self.current_slot_exchange if self.current_slot_exchange not in conflicts
                                 else NoAcceptedSlotReply.ABSENT,
                                 frozenset(conflicts), self._change_list_owner())

    def _change_list_owner(self) -> staff.StaffId | NoChangeListOwner:
        item = self.source
        if isinstance(item, review_items.ReviewItem) and isinstance(item.source, review_items.ChangeSource):
            for reference in (item.source_unit, item.owner):
                owner = _answerer(reference, self.members)
                if not isinstance(owner, RecordedAnswerer):
                    return owner
        return NoChangeListOwner.ABSENT

    def _change_slot(self, source: review_items.ReviewSource | update_registration.QuestionRecord
                     ) -> OpenQuestionSlot | AnsweredQuestionSlot | NoQuestionSlot:
        if not isinstance(source, review_items.ChangeSource):
            return NoQuestionSlot.ABSENT
        slot = source.slots.question
        if not isinstance(slot, (delivery_state.AnsweredQuestion, delivery_state.OpenQuestion)) or isinstance(
                slot, (review_items.CompletedFeedback, review_items.OpenFeedback)):
            return NoQuestionSlot.ABSENT
        if isinstance(slot, delivery_state.OpenQuestion):
            return OpenQuestionSlot(slot.question_id, slot.text)
        time = source.answered_at if isinstance(source.answered_at, datetime) else TimeNotRecorded.ABSENT
        author = self._slot_author(source, slot, time)
        return AnsweredQuestionSlot(slot.question_id, slot.text, slot.answer, time, author)

    def _slot_author(self, source: review_items.ChangeSource, slot: delivery_state.AnsweredQuestion,
                     time: datetime | TimeNotRecorded) -> staff.StaffId | RecordedAnswerer:
        validators = {_answerer(reference, self.members) for reference in source.validators}
        if source.answered_by or len(validators) == 1:
            return _answerer(source.answered_by, self.members) if source.answered_by else next(iter(validators))
        saved = self.filed.get(f"question:{slot.question_id}")
        if (saved is not None and saved.question == slot.text and isinstance(saved.answer, ExchangeAnswer)
                and saved.answer.text == slot.answer and (not isinstance(time, datetime) or saved.answer.time == time)):
            author = self._normalize_author(saved.answerer)
            if not isinstance(author, RecordedAnswerer):
                return author
        tagged = self.tagged_replies[slot.question_id]
        starts = [start for start, prompt in self.prompts.items()
                  if prompt.origin == QuestionSlot(slot.question_id) and _same_prompt(prompt.text, slot.text)]
        positions = tagged or (self.windows[starts[0]] if len(starts) == 1 else ())
        if len(positions) == 1:
            event, author = self.log[positions[0]], self.authors[positions[0]]
            if (not isinstance(author, RecordedAnswerer) and not validators - {author}
                    and event.words == "Answer: " + slot.answer
                    and (not isinstance(time, datetime) or event.time == time)):
                return author
        return list_exchange.UNRECORDED_ANSWERER

    def review_handoff(self, item: review_items.ReviewItem, key: str, destination: investigation.ListDestination,
                       saved: tuple[list_exchange.FiledExchange, ...]
                       ) -> review_items.FiledReviewExchange | review_items.NoFiledReviewExchange:
        source = self.source
        native = isinstance(source, update_registration.QuestionRecord)
        accepted = self.resolve(key, ())
        exchanges = tuple(exchange for exchange in accepted.exchanges if _eligible(exchange))
        card_can_move = native or self.report is not None
        current = {content for exchange in exchanges if (content := self._handoff_content(
            list_exchange.FiledExchange.from_exchange(exchange))) is not None}
        matched = tuple(exchange for exchange in saved if (card_can_move or isinstance(exchange.binding, ExchangeBinding))
                        and self._handoff_content(exchange) in current)
        if not matched:
            return review_items.NoFiledReviewExchange.ABSENT
        verified = tuple(exchange for exchange in matched if exchange.exchange_id in accepted.verified_ids)
        ids = frozenset(exchange.exchange_id for exchange in verified if isinstance(exchange.answer, ExchangeAnswer))
        events, slot_id = self._handoff_reply_events(item, accepted, verified)
        followups = source.item.get("followups", ()) if native else ()
        filed_followups = frozenset(index for index in range(len(followups)) if f"followup:{index + 1}" in ids)
        open_staff_question = any(exchange.direction is Direction.QUESTION_TO_NATE
                                  and exchange.answer is QuestionWithoutAnswer.OPEN for exchange in verified)
        offset = len(item.log) - len(self.log)
        filed_investigation_answer = bool(self.report is not None and self.report.answer and (
            (self.log[self.answers[-1]].words == "Answer: " + self.report.answer
             and offset + self.answers[-1] in events) if self.answers else "initial" in ids))
        sent_question = native and "review_submission" in source.item
        hides_card = (bool(verified) and (sent_question or item.state is not review_items.ReviewState.WAITING)
                      and all(offset + position in events for position in self.answers)
                      and (self._filed_native_content(verified, filed_investigation_answer) if card_can_move
                           else self._single_change_slot_card(item, slot_id, accepted)))
        return review_items.FiledReviewExchange(destination, hides_card, events, ids, open_staff_question,
                                                slot_id, filed_investigation_answer,
                                                filed_followup_indices=filed_followups)

    def _single_change_slot_card(self, item: review_items.ReviewItem, slot_id: str,
                                 accepted: AcceptedExchanges) -> bool:
        source, slot = item.source, self.slot
        return (bool(slot_id) and isinstance(source, review_items.ChangeSource)
                and item.state is review_items.ReviewState.OPEN
                and isinstance(source.slots.verdict, delivery_state.NoDelivery)
                and isinstance(slot, AnsweredQuestionSlot) and isinstance(slot.author, RecordedAnswerer)
                and len(accepted.exchanges) == 1 and not accepted.conflicting_ids)

    def _filed_native_content(self, saved: tuple[list_exchange.FiledExchange, ...],
                              filed_investigation_answer: bool) -> bool:
        if self.report is not None:
            return not self.report.answer or filed_investigation_answer
        if isinstance(record := self.source, update_registration.QuestionRecord):
            if ("review_submission" not in record.item and
                    (record.status != "closed" or any(event.who == Nate.USER and event.words.startswith("answer: ")
                                                      for event in self.log))
                    and not any(row.exchange_id == "initial" for row in saved)):
                return False
            words = {value.exchange_id: (value.question, value.answer.text)
                     for value in saved if isinstance(value.answer, ExchangeAnswer)}
            replies = record.item.get("followups", [])
            pending = record.item.get("followup")
            return (all(words.get(f"followup:{index}") == (pair["followup"], pair["reply"])
                        for index, pair in enumerate(replies, 1))
                    and ("review_submission" not in record.item or pending is None or any(
                        row.exchange_id == f"followup:{len(replies) + 1}" and row.question == pending["text"]
                        and row.answer is QuestionWithoutAnswer.OPEN for row in saved)))
        return False

    def _handoff_content(self, exchange: list_exchange.FiledExchange) -> list_exchange.FiledExchange | None:
        answer = exchange.answer
        binding = exchange.binding
        if isinstance(binding, ExchangeBinding) and isinstance(binding.reply, LoggedReply):
            position = _event_position(binding.reply, self.log)
            if position is None:
                return None
            question = binding.question
            if isinstance(question, QuestionEvent):
                prompt = _bound_event_prompt(question, exchange, position, self.prompts, self.windows)
                if prompt is None or not isinstance(prompt.origin, QuestionEvent):
                    return None
                question = replace(question, position=prompt.origin.position)
            binding = replace(binding, question=question, reply=replace(binding.reply, position=position))
        if isinstance(answer, ExchangeAnswer):
            answer = replace(answer, read=AnswerRead.UNKNOWN)
        return replace(exchange, answer=answer, binding=binding, legacy_binding="")

    def _handoff_reply_events(self, item: review_items.ReviewItem, accepted: AcceptedExchanges,
                             verified: tuple[list_exchange.FiledExchange, ...]) -> tuple[frozenset[int], str]:
        """Use the log positions of the verified exchanges."""
        offset = len(item.log) - len(self.log)
        positions: set[int] = set()
        filed_slot_id = ""
        if any(row.exchange_id == accepted.current_slot_exchange for row in verified):
            assert isinstance(slot := self.slot, AnsweredQuestionSlot)
            filed_slot_id = slot.tag
            synthetic_tag = question_path.answer_log_id(slot.tag)
            positions.update(index for index, event in enumerate(item.log[:max(0, offset)])
                             if event.feedback_request_id == synthetic_tag and event.words == "Answer: " + slot.answer)
        if offset >= 0:
            for row in verified:
                position = accepted.log_positions.get(row.exchange_id)
                if position is None or not isinstance(row.answer, ExchangeAnswer):
                    continue
                native, displayed = self.log[position], item.log[offset + position]
                if (native.words == "Answer: " + row.answer.text
                        and native == replace(displayed, time=review_items.aware(displayed.time, self.zone))):
                    positions.add(offset + position)
        return frozenset(positions), filed_slot_id


def _slot_prompt(slot: AnsweredQuestionSlot, prompts: dict[int, _Prompt], filed: datetime) -> _Prompt:
    identity = "question:" + slot.tag
    tagged = [prompt for prompt in prompts.values() if prompt.identity == identity]
    if len(tagged) == 1 and _same_prompt(tagged[0].text, slot.text):
        return tagged[0]
    return _Prompt(QuestionSlot(slot.tag), identity, slot.text, filed)


def _saved_scoped_exchange(saved: list_exchange.FiledExchange, links: tuple[ListLink, ...],
                           read_times: tuple[datetime, ...], acknowledged: bool) -> Exchange:
    answer = saved.answer
    if isinstance(answer, ExchangeAnswer) and _read_state(
            answer.time, read_times, acknowledged=acknowledged) is AnswerRead.READ:
        answer = replace(answer, read=AnswerRead.READ)
    return Exchange(saved.source, saved.source_key, saved.exchange_id, saved.direction, saved.question,
                    saved.asked_at, saved.asker, saved.answerer, answer, links, binding=saved.binding)


def _bound_event_prompt(origin: QuestionEvent, exchange: list_exchange.Exchange, position: int,
                        prompts: dict[int, _Prompt], windows: dict[int, tuple[int, ...]]) -> _Prompt | None:
    """Recover the question in the verified reply's interval."""
    matches = [prompt for index, prompt in prompts.items()
               if position in windows[index] and isinstance(prompt.origin, QuestionEvent)
               and prompt.text == exchange.question and prompt.time == exchange.asked_at]
    if len(matches) != 1:
        return None
    prompt = matches[0]
    if prompt.origin != QuestionEvent(origin.position):
        if sum(candidate.text == prompt.text and candidate.time == prompt.time
               for candidate in prompts.values()) != 1:
            return None
    return prompt


def _new_event_id(prompt: _Prompt, event: ReviewLog) -> str:
    if not isinstance(prompt.origin, QuestionEvent):
        return prompt.identity
    stamp = prompt.time.astimezone(UTC).isoformat()
    digest = hashlib.sha256("\0".join((stamp, prompt.text, event_digest(event))).encode()).hexdigest()
    return f"log:sha256:{digest}"


def _completed_feedback_reply(source: review_items.ChangeSource, slot: review_items.CompletedFeedback,
                              log: tuple[ReviewLog, ...], prompts: dict[int, _Prompt],
                              members: staff.StaffSnapshot) -> int | None:
    requests = [index for index, event in enumerate(log) if event.who == Nate.USER
                and event.words.startswith("feedback: ") and _same_prompt(event.words[10:], slot.text)
                and event.feedback_request_id in ("", slot.question_id)]
    if len(requests) != 1:
        return None
    request = requests[0]
    positions = _reply_windows(log, (*prompts, request))[request]
    if len(positions) != 1:
        return None
    position = positions[0]
    event = log[position]
    if (event.words[8:] == slot.answer and event.feedback_request_id in (
                "", slot.question_id, question_path.answer_log_id(slot.question_id))
            and not isinstance(_answerer(event.who, members), RecordedAnswerer)
            and (not source.answered_by or _is_answerer(event, source.answered_by, members))
            and (not isinstance(source.answered_at, datetime) or event.time == source.answered_at)):
        return position
    return None


_REVIEW_SOURCE_KINDS = (*review_items.QUESTION_TYPES, *review_items.INVESTIGATION_TYPES,
                       review_items.ReviewType.PROPOSAL, review_items.ReviewType.CHANGE, review_items.ReviewType.EFFICIENCY)

@dataclass
class ReviewListLinks:
    current: dict[str, list[list_page.ListRecord]] = field(default_factory=lambda: defaultdict(list))
    created: dict[str, list[list_page.ListRecord]] = field(default_factory=lambda: defaultdict(list))
    keyless: dict[investigation.ListDestination, the_list.KeylessQuestionItem] = field(default_factory=dict)
    filed_sources: set[str] = field(default_factory=set)


def _reload_review(root: Path, item: review_items.ReviewItem, members: staff.StaffSnapshot,
                   zone: ZoneInfo, *, filing: the_list.QuestionFiling) -> Iterable[Exchange]:
    try:
        current = review_items.read_current(root, item, members, include_filed=True)
    except review_items.ReviewRecordMissing:
        return ()
    links = _list_review_links(_strict_list_entries())
    return _review_item_exchanges(current, links, zone, members, the_list.exchange_history(_review_source_key(current)),
                                  filing=filing)


def _review_source_key(item: review_items.ReviewItem) -> str:
    source = item.source
    if isinstance(source, (review_items.QuestionSource, review_items.UnownedQuestionSource)):
        return _native_question_origin(source.record).source_key
    origin = review_items.submission_origin(item)
    if isinstance(origin, new_item_dialog.ReviewSubmission):
        kind = "proposal" if isinstance(source, review_items.ProposalSource) else item.type.value.lower()
        return f"submission/{kind}/{origin.request_id}"
    return item.key


def _review_item_exchanges(item: review_items.ReviewItem, links: ReviewListLinks,
                           zone: ZoneInfo, members: staff.StaffSnapshot,
                           history: tuple[list_exchange.FiledExchange, ...] = (),
                           unresolved: list[tuple[str, int]] | None = None, *,
                           filing: the_list.QuestionFiling = the_list.QuestionFiling.LIVE) -> AcceptedExchanges:
    source = item.source
    key = _review_source_key(item)
    if isinstance(source, (review_items.QuestionSource, review_items.UnownedQuestionSource)):
        return _native_question_exchanges(source.record, key, links, zone, members, history, unresolved, filing=filing)
    destinations = _linked_destinations(links.current, (item.key,))
    if (isinstance(source, review_items.InvestigationSource)
            and isinstance(destination := source.record.list_destination, investigation.ListDestination)):
        destinations = the_list.unique_destinations((destination, *destinations))
    return ExchangeVerifier(item, zone, members, history).resolve(key, destinations, unresolved)


def _native_question_exchanges(record: update_registration.QuestionRecord, key: str, links: ReviewListLinks,
                               zone: ZoneInfo, members: staff.StaffSnapshot,
                               history: tuple[list_exchange.FiledExchange, ...] = (),
                               unresolved: list[tuple[str, int]] | None = None, *,
                               filing: the_list.QuestionFiling = the_list.QuestionFiling.LIVE,
                               studio: StudioRouteOwner = StudioRouteOwner.OTHER_WRITER) -> AcceptedExchanges:
    origin = _native_question_origin(record)
    route = resolve_legacy_question_destination(origin, links, record=record, filing=filing, studio=studio)
    destinations = (route.destination,) if isinstance(route, ResolvedQuestionDestination) else ()
    accepted = ExchangeVerifier(record, zone, members, history).resolve(key, destinations, unresolved)
    return replace(accepted, exchanges=tuple(replace(exchange, question_route=route) for exchange in accepted))


def _studio_question_route(folder: Path, origin: the_list.NativeQuestionItem,
                           verified: tuple[investigation.ListDestination, ...],
                           current: tuple[investigation.ListDestination, ...]
                           ) -> ResolvedQuestionDestination | Literal[QuestionRoute.NEW, QuestionRoute.STAGED_STUDIO]:
    """Read the saved Studio route and its List reservation."""
    destination = the_list.review_reservation(folder / "list-number",
                                              source_destinations=verified, current_links=current)
    path = folder / "list-filing.json"
    if not record_store.saved_path_exists(path):
        if destination is the_list.NextNumber.AVAILABLE:
            return QuestionRoute.NEW
        raise the_list.ListError(f"Cannot find the List route for request {folder.name}.")
    fields = cast(object, json.loads(path.read_text(encoding="utf-8")))
    match fields:
        case {"skipped": str()} if destination is the_list.NextNumber.AVAILABLE:
            return (QuestionRoute.STAGED_STUDIO if record_store.saved_path_exists(folder / "first-message.json")
                    else QuestionRoute.NEW)
        case {"keeper": str(keeper)} if keeper:
            new_item_dialog.check_recipient_id(staff.StaffId(keeper))
            if destination is the_list.NextNumber.AVAILABLE:
                return QuestionRoute.STAGED_STUDIO
            return ResolvedQuestionDestination(destination, origin)
        case {"unavailable": str()} if destination is the_list.NextNumber.AVAILABLE:
            return QuestionRoute.STAGED_STUDIO
        case _:
            raise the_list.ListError(f"Invalid List route for request {folder.name}.")


def _moved_question_link(path: Path, record: update_registration.QuestionRecord,
                         address: review_submissions.SubmissionAddress, payload_hash: str) -> str:
    receipt = review_submissions._read_request_file(path, path.parent.name)  # pyright: ignore[reportPrivateUsage]
    if (not isinstance(receipt, review_submissions.NativeSubmissionDestination) or receipt.address != address
            or receipt.owner != record.owner or receipt.id != record.item.get("review_id")
            or receipt.payload_hash != payload_hash or receipt.path != record.path.resolve()
            or receipt.original_path == receipt.path):
        return ""
    fields = cast(object, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(fields, dict):
        return ""
    move = cast(dict[str, object], fields)
    match move:
        case {"source": str(source), "source_owner": str(owner)} if Path(source).is_absolute():
            if Path(source) == receipt.original_path:
                original_owner = staff.StaffId(owner)
                new_item_dialog.check_recipient_id(original_owner)
                return f"question/{original_owner}/{receipt.original_path.name}"
            folders = record_store.known_folders(record_store.RecordKind.QUESTION, receipt.recipient_id)
            if receipt.original_path.parent in folders:
                return f"question/{receipt.recipient_id}/{receipt.original_path.name}"
        case _:
            pass
    return ""


class NativeQuestionRecord(StrEnum):
    ABSENT = "no native Review record for this question"


def _native_question_origin(record: update_registration.QuestionRecord) -> the_list.NativeQuestionItem:
    submission = review_submissions.native_origin(record)
    if isinstance(submission, new_item_dialog.ReviewSubmission):
        return the_list.NativeQuestionItem(the_list.QuestionItemKind.FROM_NATE,
                                           f"submission/question/{submission.request_id}")
    return the_list.NativeQuestionItem(the_list.QuestionItemKind.TO_NATE, review_items.question_record_key(record))


def _resolve_current_question_destination(
    record: update_registration.QuestionRecord, origin: the_list.NativeQuestionItem,
    *, studio: StudioRouteOwner = StudioRouteOwner.OTHER_WRITER,
) -> ResolvedQuestionDestination | UnfinishedQuestionReservation | QuestionRoute:
    """Read the List under the caller's question filing locks."""
    try:
        entries = _strict_list_entries()
    except the_list.IncompleteListScan as error:
        raise the_list.IncompleteListScan(f"Incomplete List destination check for {origin.source_key}: {error}") from error
    route = resolve_legacy_question_destination(origin, _list_review_links(entries), record=record,
                                                filing=the_list.QuestionFiling.LIVE, studio=studio)
    if isinstance(route, ResolvedQuestionDestination) and not any(
            the_list.same_destination(investigation.ListDestination(entry.keeper, entry.item.number), route.destination)
            for entry in entries):
        return UnfinishedQuestionReservation(route.destination)
    return route


def resolve_legacy_question_destination(
    origin: the_list.NativeQuestionItem, links: ReviewListLinks, *,
    record: update_registration.QuestionRecord | NativeQuestionRecord = NativeQuestionRecord.ABSENT,
    filing: the_list.QuestionFiling = the_list.QuestionFiling.HISTORY,
    studio: StudioRouteOwner = StudioRouteOwner.OTHER_WRITER,
) -> ResolvedQuestionDestination | QuestionRoute:
    """Resolve the original List destination from durable question records."""
    request = origin.source_key.removeprefix("submission/question/")
    folder: Path | None = None
    studio_route = QuestionRoute.NEW
    if origin.kind is the_list.QuestionItemKind.FROM_NATE:
        try:
            folder = review_submissions._request_folder(request)  # pyright: ignore[reportPrivateUsage]
            source_keys = [origin.source_key]
            if isinstance(record, update_registration.QuestionRecord):
                source_keys.append(new_item_dialog.submission_key(
                    new_item_dialog.SubmissionKind.QUESTION, record.owner, record.item.get("review_id", ""), request))
            verified = the_list.unique_destinations(entry.destination for key in source_keys
                for entry in links.created.get(key, ()) if the_list.original_question_link(entry.item))
            current = _linked_destinations(links.current, source_keys)
            saved_route = _studio_question_route(folder, origin, verified, current)
        except (OSError, ValueError):
            return QuestionRoute.UNRESOLVED
        if isinstance(saved_route, ResolvedQuestionDestination):
            return saved_route
        if saved_route is QuestionRoute.STAGED_STUDIO and studio is StudioRouteOwner.OTHER_WRITER:
            return saved_route
        studio_route = saved_route

    try:
        exchange = the_list.exchange_reservation(origin.source_key)
    except (OSError, ValueError):
        return QuestionRoute.UNRESOLVED
    if isinstance(exchange, the_list.ExchangeDestination) and exchange.reserved_new_item:
        return ResolvedQuestionDestination(exchange.destination, origin)

    keys = (origin.source_key,)
    move_markers: set[str] = set()
    if isinstance(record, update_registration.QuestionRecord):
        keys = tuple(dict.fromkeys((*keys, review_items.question_record_key(record))))
        if origin.kind is the_list.QuestionItemKind.FROM_NATE:
            keys += (new_item_dialog.submission_key(
                new_item_dialog.SubmissionKind.QUESTION, record.owner, record.item.get("review_id", ""), request),)
        move_markers = {event.words for event in review_events.decode(record.item.get("review_log", ""))
                        if review_items.list_move_event(event)}
    created = _linked_destinations(links.created, keys)
    if created:
        if len(created) != 1:
            return QuestionRoute.UNRESOLVED
        return ResolvedQuestionDestination(created[0], origin)

    if move_markers:
        reports = the_list.unique_destinations(investigation.ListDestination(entry.keeper, entry.item.number)
                   for key in keys for entry in links.current.get(key, ())
                   if isinstance(report := entry.item.report, the_list.ListReport)
                   and report.kind is the_list.ReportKind.QUESTION and report.evidence_key == key
                   and f"moved to List item {entry.item.number}" in move_markers)
        if len(reports) == 1:
            return ResolvedQuestionDestination(next(iter(reports)), the_list.NoQuestionItem.ORDINARY)
        if reports:
            return QuestionRoute.UNRESOLVED

    transferred = False
    has_receipt = False
    if folder is not None:
        has_receipt = any((folder / name).exists() for name in ("receipt.json", "pending.json", "move.json", "moved.json"))
        transferred = (folder / "moved.json").exists() or (folder / "move.json").exists()
        if isinstance(record, update_registration.QuestionRecord):
            submission = review_submissions.native_origin(record)
            if isinstance(submission, new_item_dialog.ReviewSubmission):
                try:
                    recipient = new_item_dialog.request_recipient(new_item_dialog.SubmissionKind.QUESTION, request)
                except new_item_dialog.SubmissionError:
                    recipient = record.owner
                transferred = transferred or recipient != record.owner
                if (folder / "moved.json").is_file():
                    address = review_submissions.SubmissionAddress(new_item_dialog.SubmissionKind.QUESTION, recipient, request)
                    try:
                        original = _moved_question_link(folder / "moved.json", record, address, submission.request_digest)
                    except (OSError, ValueError):
                        return QuestionRoute.UNRESOLVED
                    if original:
                        created = _linked_destinations(links.created, (original,))
                        original_rows = created or _linked_destinations(links.current, (original,))
                        if len(original_rows) == 1:
                            primary = origin if created else the_list.NoQuestionItem.ORDINARY
                            return ResolvedQuestionDestination(original_rows[0], primary)
                try:
                    receipt = review_submissions._read_request_file(folder / "receipt.json", folder.name)  # pyright: ignore[reportPrivateUsage]
                except (OSError, ValueError):
                    return QuestionRoute.UNRESOLVED
                if isinstance(receipt, review_submissions.NativeSubmissionDestination):
                    transferred = transferred or (receipt.original_path != receipt.path
                                                   or receipt.original_path.name != record.path.name)

    if isinstance(exchange, the_list.ExchangeDestination):
        return ResolvedQuestionDestination(exchange.destination, the_list.NoQuestionItem.ORDINARY)
    if transferred or move_markers:
        return QuestionRoute.UNRESOLVED
    if origin.source_key in links.filed_sources or the_list.exchange_filing_started(origin.source_key):
        return QuestionRoute.UNRESOLVED
    if has_receipt and filing is the_list.QuestionFiling.HISTORY:
        return studio_route if studio_route is QuestionRoute.STAGED_STUDIO else QuestionRoute.UNRESOLVED
    current = _linked_destinations(links.current, keys)
    if len(current) == 1 and studio_route is QuestionRoute.NEW:
        return ResolvedQuestionDestination(current[0], the_list.NoQuestionItem.ORDINARY)
    return QuestionRoute.UNRESOLVED if current else studio_route


def _list_exchanges(entry: list_page.ListRecord, members: staff.StaffSnapshot, zone: ZoneInfo) -> Iterator[Exchange]:
    item = entry.item
    unread = the_list.unread_answer_ids(item)
    answerers = {event.what.question_id: event.source for event in item.log
                 if isinstance(event.what, the_list.QuestionAnswered)}
    link = investigation.ListDestination(entry.keeper, item.number)
    for question in item.questions:
        if (answer := question.answer) is None:
            continue
        answerer: staff.StaffId | RecordedAnswerer = question.recipient_id(entry.keeper)
        if name := answerers.get(question.question_id):
            member = members.by_member_reference(name)
            if member is None:
                matches = [row for row in (*members.rows, *members.retired_producers) if row.name == name]
                member = matches[0] if len(matches) == 1 else None
            answerer = member.id if member is not None else RecordedAnswerer(name)
        state = AnswerRead.UNREAD if question.question_id in unread else AnswerRead.READ
        yield Exchange(Source.LIST, f"list/{entry.key}", question.question_id, Direction.ANSWER_TO_NATE,
                       question.words, review_items.moment(question.asked_at, zone), Nate.USER, answerer,
                       ExchangeAnswer(answer.text, review_items.moment(answer.answered_at, zone), state), (link,),
                       binding=ExchangeBinding(QuestionRecord(question.question_id), ReplyWithoutEvent.NATIVE_RECORD))


def _picture_links(record: image_store.ImageRecord, keepers: dict[int, list[staff.StaffId]]) -> tuple[ListLink, ...]:
    if "list_item" not in record:
        return ()
    number = int(record["list_item"][1:])
    if "list_keeper" in record:
        return (investigation.ListDestination(staff.StaffId(record["list_keeper"]), number),)
    candidates = keepers.get(number, [])
    if len(candidates) == 1:
        return (investigation.ListDestination(candidates[0], number),)
    return (LegacyListLink(number),)


def _picture_exchanges(root: Path, keepers: dict[int, list[staff.StaffId]], zone: ZoneInfo,
                       records: Iterable[image_store.ImageRecord]) -> Iterator[Exchange]:
    directory = images.images_root(root)
    for record in records:
        answers = [stored for stored in record.get("questions", []) if stored["answer"] is not None]
        if not answers:
            continue
        identity = record["id"]
        links = _picture_links(record, keepers)
        read: set[str] = set()
        try:
            feedback = image_store.read_answers(directory, identity)
        except FileNotFoundError:
            pass
        else:
            if (feedback["owner"], feedback["registered_at"]) == (record["owner"], record["registered_at"]):
                read.update(question_id for attempt in feedback["attempts"].values()
                            if attempt["recipients"] and set(attempt["recipients"]) <= set(attempt["confirmed"])
                            for question_id in attempt["answered"])
        marks = (review_items.moment(record["feedback_at"], zone),) if "feedback_at" in record else ()
        for stored in answers:
            question = question_record.read(stored)
            answer = question.answer
            assert answer is not None
            time = review_items.moment(answer.answered_at, zone)
            state = _read_state(time, marks, acknowledged=question.question_id in read)
            yield Exchange(Source.PICTURE, f"image/{identity}", question.question_id, Direction.ANSWER_TO_NATE,
                           question.words, review_items.moment(question.asked_at, zone), Nate.USER,
                           question.recipient_id(staff.StaffId(record["owner"])), ExchangeAnswer(answer.text, time, state), links,
                           binding=ExchangeBinding(QuestionRecord(question.question_id), ReplyWithoutEvent.NATIVE_RECORD))


def _reload_list(keeper: staff.StaffId, identity: str, members: staff.StaffSnapshot,
                 zone: ZoneInfo) -> Iterator[Exchange]:
    try:
        item = the_list.load(keeper, identity)
    except FileNotFoundError:
        return
    yield from _list_exchanges(list_page.ListRecord(keeper, item), members, zone)


def _reload_picture(root: Path, identity: str, zone: ZoneInfo) -> Iterator[Exchange]:
    try:
        current = image_store.read_record(images.images_root(root) / identity)
    except FileNotFoundError:
        return
    keepers: dict[int, list[staff.StaffId]] = defaultdict(list)
    for entry in _strict_list_entries():
        keepers[entry.item.number].append(entry.keeper)
    yield from _picture_exchanges(root, keepers, zone, (current,))


def read_exchanges(root: Path, *, members: staff.StaffSnapshot) -> ExchangeInventory:
    """Read one inventory without changing the records or reserving List numbers."""
    zone = user_zone()
    reviews = review_items.read(root, members=members, kinds=_REVIEW_SOURCE_KINDS, zone=zone,
                                sources=[], include_filed=True)
    return _read_exchanges(root, members, zone, reviews)


def _read_exchanges(root: Path, members: staff.StaffSnapshot, zone: ZoneInfo,
                    reviews: Iterable[review_items.ReviewItem]) -> ExchangeInventory:
    entries, links = _migration_list_records()
    list_items: set[investigation.ListDestination] = set()
    keepers: dict[int, list[staff.StaffId]] = defaultdict(list)
    for entry in entries:
        item = entry.item
        destination = investigation.ListDestination(entry.keeper, item.number)
        list_items.add(destination)
        keepers[item.number].append(entry.keeper)
    filed = tuple((investigation.ListDestination(entry.keeper, entry.item.number), exchange)
                  for entry in entries for exchange in entry.item.filed_exchanges)
    previous: dict[str, list[list_exchange.FiledExchange]] = defaultdict(list)
    for _, exchange in filed:
        previous[exchange.source_key].append(exchange)
    exchanges: list[Exchange] = []
    unresolved: list[tuple[str, int]] = []
    reload: dict[str, ReloadExchanges] = {}
    reviews = tuple(reviews)
    review_questions = {_review_source_key(item): item.source.record for item in reviews
                        if isinstance(item.source, (review_items.QuestionSource, review_items.UnownedQuestionSource))}
    keyless: list[KeylessQuestionMigration] = []
    for destination, row in links.keyless.items():
        origin = _keyless_question_origin(destination, links, review_questions, row)
        keyless.append(KeylessQuestionMigration(destination, row, origin))
    unresolved_rows = [migration for migration in keyless if migration.origin is the_list.NoQuestionItem.ORDINARY]
    unresolved_paths = {the_list.destination_key(migration.destination) for migration in unresolved_rows}
    blocked = {origin.source_key for migration in unresolved_rows for origin in migration.row.candidate_origins}
    for item in reviews:
        key = _review_source_key(item)
        history = the_list.combine_exchange_history(tuple(previous.get(key, ())), the_list.pending_exchanges(key))
        candidates = tuple(_review_item_exchanges(item, links, zone, members, history, unresolved,
                                                  filing=the_list.QuestionFiling.HISTORY))
        if candidates:
            if key in blocked or any(_link_key(link) in unresolved_paths for exchange in candidates for link in exchange.list_links):
                candidates = tuple(replace(exchange, question_route=QuestionRoute.UNRESOLVED) for exchange in candidates)
            reload[key] = partial(_reload_review, root, item, members, zone, filing=the_list.QuestionFiling.HISTORY)
            exchanges.extend(candidates)
    for entry in entries:
        if the_list.destination_key(entry.destination) in unresolved_paths:
            continue
        candidates = tuple(_list_exchanges(entry, members, zone))
        if candidates:
            reload[candidates[0].source_key] = partial(_reload_list, entry.keeper, entry.item.id, members, zone)
            exchanges.extend(candidates)
    for record in images.records(root):
        candidates = tuple(_picture_exchanges(root, keepers, zone, (record,)))
        if candidates:
            reload[candidates[0].source_key] = partial(_reload_picture, root, record["id"], zone)
            exchanges.extend(candidates)
    ordered = sorted(exchanges, key=lambda exchange: (exchange.source, exchange.source_key, exchange.exchange_id))
    return ExchangeInventory(tuple(ordered), frozenset(list_items), filed, reload, tuple(sorted(unresolved)),
                             tuple(keyless), review_questions)


def _count(inventory: ExchangeInventory) -> Iterator[str]:
    rows = {the_list.destination_key(destination) for destination in inventory.list_items}
    for source in Source:
        exchanges = [exchange for exchange in inventory.exchanges if exchange.source is source]
        records = {exchange.source_key: exchange.list_links for exchange in exchanges}
        linked = sum(bool(links) for links in records.values())
        states = [exchange.answer.read for exchange in exchanges if isinstance(exchange.answer, ExchangeAnswer)]
        unknown = states.count(AnswerRead.UNKNOWN)
        missing = len(records) - linked
        line = (f"{source}: records={len(records)} exchanges={len(exchanges)} "
                f"unread={states.count(AnswerRead.UNREAD)} read={states.count(AnswerRead.READ)} "
                f"linked={linked} new-rows={missing}")
        yield line + (f" unknown-read-state={unknown}" if unknown else "")
        for key, links in records.items():
            for link in links:
                if isinstance(link, LegacyListLink):
                    yield f"  Bad List link: {key} -> L{link.number} (no unique keeper)"
                elif the_list.destination_key(link) not in rows:
                    yield f"  Bad List link: {key} -> {link.keeper}/L{link.number} (item missing)"


def _eligible(exchange: Exchange) -> bool:
    return exchange.answer is not QuestionWithoutAnswer.CLOSED


@dataclass(frozen=True)
class FilingPlan:
    exchanges: tuple[Exchange, ...]
    destinations: tuple[ListLink, ...]
    previously_filed: bool

    @property
    def question_origin(self) -> the_list.NativeQuestionItem | the_list.NoQuestionItem:
        return self.exchanges[0].question_origin

    @property
    def unresolved_question_destination(self) -> bool:
        return self.exchanges[0].question_route in (QuestionRoute.STAGED_STUDIO, QuestionRoute.UNRESOLVED)


def _plans(exchanges: Iterable[Exchange], filed: Iterable[tuple[investigation.ListDestination, list_exchange.FiledExchange]]
           ) -> Iterator[FilingPlan]:
    existing: dict[str, dict[tuple[Path, int], investigation.ListDestination]] = defaultdict(dict)
    for destination, exchange in filed:
        _ = existing[exchange.source_key].setdefault(the_list.destination_key(destination), destination)
    groups: dict[str, list[Exchange]] = defaultdict(list)
    for exchange in exchanges:
        if _eligible(exchange) or exchange.source_key in existing:
            groups[exchange.source_key].append(exchange)
    for key, group in groups.items():
        route = group[0].question_route
        if isinstance(route, ResolvedQuestionDestination):
            destinations: tuple[ListLink, ...] = (route.destination,)
        elif route is QuestionRoute.ORDINARY:
            links = (link for exchange in group for link in exchange.list_links)
            destinations = _unique_links((*links, *existing[key].values()))
        else:
            destinations = ()
        yield FilingPlan(tuple(group), destinations, bool(existing[key]))


class NoExchangeFiling(StrEnum):
    SOURCE_NO_LONGER_ELIGIBLE = "The source no longer has eligible exchanges."
    UNRESOLVED_QUESTION = "The question has no verified List destination."


def file_source(plan: FilingPlan, members: staff.StaffSnapshot, reload: ReloadExchanges, *,
                question_filing: the_list.QuestionFiling = the_list.QuestionFiling.LIVE,
                submission_question: the_list.NativeQuestionItem | the_list.NoQuestionItem = the_list.NoQuestionItem.ORDINARY
                ) -> investigation.ListDestination | NoExchangeFiling:
    """Read the current source under its filing lock before saving to the List."""
    planned = plan.exchanges[0]
    key = planned.source_key
    with ExitStack() as locks:
        if planned.source is Source.STAFF_QUESTION:
            # Withdrawal holds this lock when it saves the native question.
            _, owner, _ = key.split("/", 2)
            path = update_registration.outstanding_path_for_owner(staff.StaffId(owner))
            _ = locks.enter_context(update_registration.outstanding_lock(path))
        source = locks.enter_context(the_list.lock_exchange_source(key))
        current = reload()
        exchanges = tuple(exchange for exchange in current
                          if exchange.source_key == key and (_eligible(exchange) or plan.previously_filed))
        if not exchanges:
            return NoExchangeFiling.SOURCE_NO_LONGER_ELIGIBLE
        first = next((exchange for exchange in exchanges if exchange.direction is Direction.QUESTION_TO_NATE),
                     exchanges[0])
        route = first.question_route
        if (plan.unresolved_question_destination or route in (QuestionRoute.STAGED_STUDIO, QuestionRoute.UNRESOLVED)
                or (isinstance(planned.question_route, ResolvedQuestionDestination) and route is QuestionRoute.NEW)):
            return NoExchangeFiling.UNRESOLVED_QUESTION
        if isinstance(route, ResolvedQuestionDestination):
            destinations: tuple[ListLink, ...] = (route.destination,)
        elif route is QuestionRoute.ORDINARY:
            destinations = _unique_links(link for exchange in exchanges for link in exchange.list_links)
        else:
            destinations = ()
        if len(destinations) > 1 or any(isinstance(link, LegacyListLink) for link in destinations):
            raise the_list.ListError("The source has no unique List destination.")
        link = destinations[0] if destinations else the_list.NextNumber.AVAILABLE
        assert not isinstance(link, LegacyListLink)
        actor = first.asker if first.direction is Direction.QUESTION_TO_NATE else first.answerer
        if (not isinstance(link, investigation.ListDestination)
                and isinstance(actor, RecordedAnswerer) and isinstance(current, AcceptedExchanges)
                and first.exchange_id == current.current_slot_exchange):
            owner = current.change_list_owner
            if isinstance(owner, NoChangeListOwner):
                owner = the_list.report_route(staff.StaffId(""), members).keeper
        elif not isinstance(link, investigation.ListDestination) and isinstance(actor, (Nate, RecordedAnswerer)):
            raise the_list.ListError("The source has no staff ID for the List owner.")
        else:
            owner = staff.StaffId(actor) if not isinstance(actor, RecordedAnswerer) else staff.StaffId("")
        if not first.question.strip():
            raise the_list.ListError("The source question has no text.")
        title = review_submissions.submission_title(first.question)
        if submission_question is the_list.NoQuestionItem.ORDINARY:
            submission_question = first.question_origin
        try:
            conflicts: frozenset[str] = current.conflicting_ids if isinstance(current, AcceptedExchanges) else frozenset()
            return the_list.file_exchanges(source, exchanges, title, linked=link, owner=owner, members=members,
                                           question_filing=question_filing, unresolved_reply_ids=conflicts,
                                           submission_question=submission_question)
        except the_list.ConflictingExchangeReplies:
            return NoExchangeFiling.SOURCE_NO_LONGER_ELIGIBLE


def _file_live(reload: ReloadExchanges, members: staff.StaffSnapshot, key: str) -> None:
    try:
        entries = _strict_list_entries()
        filed = ((investigation.ListDestination(entry.keeper, entry.item.number), exchange)
                 for entry in entries for exchange in entry.item.filed_exchanges if exchange.source_key == key)
        plans = tuple(_plans(reload(), filed))
    except (OSError, ValueError) as error:
        print(f"List exchange {key}: {error}", file=sys.stderr)
        return
    for plan in plans:
        key = plan.exchanges[0].source_key
        try:
            result = file_source(plan, members, reload)
        except (OSError, ValueError) as error:
            print(f"List exchange {key}: {error}", file=sys.stderr)
        else:
            if result is NoExchangeFiling.UNRESOLVED_QUESTION:
                print(f"List exchange {key}: {result}", file=sys.stderr)


def _list_review_links(entries: Iterable[list_page.ListRecord]) -> ReviewListLinks:
    links = ReviewListLinks()
    for entry in entries:
        links.filed_sources.update(exchange.source_key for exchange in entry.item.filed_exchanges)
        keys = set(entry.item.reviews)
        if isinstance(entry.item.report, the_list.ListReport):
            keys.add(entry.item.report.evidence_key)
        for key in keys:
            links.current[key].append(entry)
        if key := the_list.original_question_link(entry.item):
            links.created[key].append(entry)
            if isinstance(origin := the_list.question_origin_from_link(key), the_list.NativeQuestionItem):
                links.created[origin.source_key].append(entry)
    return links


def _strict_list_entries() -> list[list_page.ListRecord]:
    return [list_page.ListRecord(row.keeper, row.item) for row in the_list.scan_list_records()]


def _migration_list_records() -> tuple[list[list_page.ListRecord], ReviewListLinks]:
    entries = _strict_list_entries()
    links = _list_review_links(entries)
    links.keyless.update({investigation.ListDestination(entry.keeper, entry.item.number):
                          the_list.KeylessQuestionItem(entry.item) for entry in entries
                          if isinstance(entry.item.keyless_question_kind, the_list.QuestionItemKind)})
    return entries, links


def move_question_wait(root: Path, item: review_items.ReviewItem, members: staff.StaffSnapshot,
                       owner: staff.StaffId) -> investigation.ListDestination | investigation.NoListDestination:
    """File a Question report under the same locks as exchange filing."""
    with investigation.recipient_locks(owner), update_registration.wrap_question_locks(owner):
        try:
            current = review_items.read_current(root, item, members, include_filed=True)
        except review_items.ReviewRecordMissing:
            return investigation.NoListDestination.ABSENT
        if not review_items.can_move_wait(current):
            return investigation.NoListDestination.ABSENT
        assert isinstance(current.source, (review_items.QuestionSource, review_items.UnownedQuestionSource))
        record = current.source.record
        origin = _native_question_origin(record)
        with the_list.lock_exchange_source(origin.source_key):
            resolved = _resolve_current_question_destination(record, origin)
            if isinstance(resolved, UnfinishedQuestionReservation) or resolved in (
                    QuestionRoute.STAGED_STUDIO, QuestionRoute.UNRESOLVED):
                return investigation.NoListDestination.ABSENT
            if the_list.exchange_reservation(origin.source_key) is not the_list.NextNumber.AVAILABLE:
                return investigation.NoListDestination.ABSENT
            route = the_list.report_route(current.owner, members)
            keeper = route.keeper
            target: investigation.ListDestination | the_list.NewReportItem = the_list.NewReportItem.ALLOCATE
            if isinstance(resolved, ResolvedQuestionDestination):
                if isinstance(resolved.origin, the_list.NativeQuestionItem):
                    return investigation.NoListDestination.ABSENT
                target = resolved.destination
                keeper = target.keeper
                previous = the_list.load(keeper, f"L{target.number}")
                if any(exchange.source_key == origin.source_key for exchange in previous.filed_exchanges):
                    return investigation.NoListDestination.ABSENT
                if isinstance(previous.report, the_list.ListReport):
                    if previous.report.kind is not the_list.ReportKind.QUESTION or previous.report.evidence_key != current.key:
                        return investigation.NoListDestination.ABSENT
                elif current.key not in previous.reviews or previous.state is the_list.ItemState.CLOSED:
                    return investigation.NoListDestination.ABSENT
            report = the_list.ListReport(the_list.ReportKind.QUESTION, current.key, record.item["text"])
            destination = the_list.file_report(
                keeper, report,
                ' '.join(current.title.split()), owner=current.owner or the_list.UnassignedOwner.NOT_RECORDED,
                producer=route.producer, unit=route.unit, source='moved from Review',
                state=the_list.ItemState.WORKING, reservation=target)
            the_list.verify_report_destination(destination, report)
            update_registration.record_review_log(record, f'moved to List item {destination.number}')
            return destination


def _resolved_question_origin(destination: investigation.ListDestination, links: ReviewListLinks,
                              records: dict[str, update_registration.QuestionRecord],
                              candidates: Iterable[the_list.NativeQuestionItem]) -> the_list.NativeQuestionItem | the_list.NoQuestionItem:
    resolved: set[the_list.NativeQuestionItem] = set()
    for origin in candidates:
        route = resolve_legacy_question_destination(origin, links,
            record=records.get(origin.source_key, NativeQuestionRecord.ABSENT))
        if (isinstance(route, ResolvedQuestionDestination) and route.origin == origin
                and the_list.same_destination(route.destination, destination)):
            resolved.add(origin)
    return next(iter(resolved)) if len(resolved) == 1 else the_list.NoQuestionItem.ORDINARY


def _keyless_question_origin(destination: investigation.ListDestination, links: ReviewListLinks,
                             records: dict[str, update_registration.QuestionRecord], row: the_list.KeylessQuestionItem
                             ) -> the_list.NativeQuestionItem | the_list.NoQuestionItem:
    return _resolved_question_origin(destination, links, records, row.candidate_origins)


def _linked_destinations(links: dict[str, list[list_page.ListRecord]],
                         keys: Iterable[str]) -> tuple[investigation.ListDestination, ...]:
    return the_list.unique_destinations(investigation.ListDestination(entry.keeper, entry.item.number)
                                        for key in keys for entry in links.get(key, ()))


def file_saved_question(record: update_registration.QuestionRecord, members: staff.StaffSnapshot) -> None:
    physical = review_items.question_record_key(record)
    with review_items.after_store_write(physical):
        zone = user_zone()

        def reload() -> Iterable[Exchange]:
            for current in update_registration.selected_question_records(
                    [record.path], ("open", "closed"), None, owner=record.owner):
                key = _native_question_origin(current).source_key
                links = _list_review_links(_strict_list_entries())
                return _native_question_exchanges(current, key, links, zone, members, the_list.exchange_history(key))
            return ()

        _file_live(reload, members, _native_question_origin(record).source_key)


def file_saved_submission(scoped: new_item_dialog.ScopedSubmissionKey, members: staff.StaffSnapshot) -> None:
    with review_items.after_store_write(scoped.item_id):
        with review_submissions.scoped_submission(scoped, members=members) as current:
            saved = current.saved()
        file_saved_review(review_items.submission_item(saved, members), members)


def _confirm_list_submission_question(destination: investigation.ListDestination,
                                      origin: the_list.NativeQuestionItem) -> None:
    if the_list.load(destination.keeper, f"L{destination.number}").native_question != origin:
        raise OSError(f"Cannot confirm the question source for L{destination.number}.")


def finish_studio_question_without_keeper(record: update_registration.QuestionRecord,
                                          save_fallback: Callable[[], None]) -> None:
    """Check the destination and finish routing under the exchange lock."""
    origin = _native_question_origin(record)
    with the_list.lock_exchange_source(origin.source_key):
        match _resolve_current_question_destination(record, origin, studio=StudioRouteOwner.STUDIO_SEND):
            case ResolvedQuestionDestination(destination=destination):
                _confirm_list_submission_question(destination, origin)
            case QuestionRoute.NEW | QuestionRoute.STAGED_STUDIO:
                save_fallback()
            case _:
                raise OSError(f"Cannot resolve the List destination for {origin.source_key}.")


def file_list_submission_question(record: update_registration.QuestionRecord,
                                  reservation: Path, file_row: Callable[[], investigation.ListDestination],
                                  members: staff.StaffSnapshot) -> None:
    """File the question under the recipient locks before the first notice."""
    origin = _native_question_origin(record)
    key = origin.source_key
    with the_list.lock_exchange_source(key):
        route = _resolve_current_question_destination(record, origin, studio=StudioRouteOwner.STUDIO_SEND)
        if route in (QuestionRoute.NEW, QuestionRoute.STAGED_STUDIO):
            destination = file_row()
        elif isinstance(route, (ResolvedQuestionDestination, UnfinishedQuestionReservation)):
            destination = route.destination
            if isinstance(route, UnfinishedQuestionReservation):
                reserved = the_list.review_reservation(reservation)
                if isinstance(reserved, investigation.ListDestination) and the_list.same_destination(reserved, destination):
                    destination = file_row()
        else:
            raise OSError(f"Cannot resolve the List destination for {key}.")

    if not record.path.is_file():
        raise OSError(f"Cannot find the saved question {key}.")
    zone = user_zone()
    entries = _strict_list_entries()

    def reload() -> AcceptedExchanges:
        return _native_question_exchanges(record, key, _list_review_links(_strict_list_entries()), zone, members,
                                          the_list.exchange_history(key),
                                          studio=StudioRouteOwner.STUDIO_SEND)

    filed = ((destination, exchange) for entry in entries
             if the_list.same_destination(entry.destination, destination)
             for exchange in entry.item.filed_exchanges)
    for plan in _plans(reload(), filed):
        filed_at = file_source(plan, members, reload)
        if isinstance(filed_at, investigation.ListDestination) and the_list.same_destination(filed_at, destination):
            break
    else:
        if update_registration.pending_review_reply(record):
            raise OSError(f"Cannot file the open question on L{destination.number}.")
    _confirm_list_submission_question(destination, origin)


def file_saved_investigation(record: investigation.Investigation) -> None:
    with review_items.after_store_write(review_items.investigation_record_key(record)):
        members = staff.snapshot()
        path = investigation.review_record_path(record.owner, record.id)
        file_saved_review(review_items.investigation_item(record, members, path), members)


def file_saved_review(item: review_items.ReviewItem, members: staff.StaffSnapshot,
                      root: Path | None = None) -> None:
    zone = user_zone()
    _file_live(partial(_reload_review, root or change_routing.review_root(), item, members, zone,
                       filing=the_list.QuestionFiling.LIVE),
               members, _review_source_key(item))


def file_saved_list(keeper: staff.StaffId, item: the_list.ListItem) -> None:
    with review_items.after_store_write(f"{keeper}/{item.id}"):
        members = staff.snapshot()
        _file_live(partial(_reload_list, keeper, item.id, members, user_zone()), members, f"list/{keeper}/{item.id}")


def file_saved_picture(root: Path, identity: str) -> None:
    with review_items.after_store_write(f"image/{identity}"):
        _file_live(partial(_reload_picture, root, identity, user_zone()), staff.snapshot(), f"image/{identity}")


def _moved_report(item: review_items.ReviewItem, candidates: Iterable[list_page.ListRecord],
                  handoff: review_items.FiledReviewExchange | review_items.NoFiledReviewExchange,
                  sources: list[Path] | None) -> investigation.ListDestination | investigation.NoListDestination:
    absent = investigation.NoListDestination.ABSENT
    source = item.source
    if isinstance(source, review_items.InvestigationSource):
        record = source.record
        destination = record.list_destination
        if not isinstance(destination, investigation.ListDestination):
            return absent
        kind = the_list.investigation_report_kind(record)
        if not isinstance(kind, the_list.ReportKind):
            kind = the_list.ReportKind.BUG if record.kind is investigation.InvestigationKind.BUG else the_list.ReportKind.INVESTIGATION
        try:
            if sources is not None:
                sources.append(the_list.folder(destination.keeper))
            row = the_list.load(destination.keeper, f"L{destination.number}", sources=sources)
        except (OSError, ValueError):
            return absent
        report = row.report
        if not (isinstance(report, the_list.ListReport) and report.kind is kind
                and (report.evidence_key == item.key or record.submission_scope
                     and report.submission_scope == record.submission_scope)):
            return absent
        for entry in candidates:
            shown = investigation.ListDestination(entry.keeper, entry.item.number)
            if entry.item.report == report and the_list.same_destination(shown, destination):
                return shown
        return absent

    marker = next((event.words for event in reversed(item.log) if review_items.list_move_event(event)), "")
    if not marker:
        return absent
    expected: tuple[the_list.ReportKind, ...]
    match item.type:
        case review_items.ReviewType.QUESTION | review_items.ReviewType.RESPONSE:
            expected = (the_list.ReportKind.QUESTION,)
            assert isinstance(source, (review_items.QuestionSource, review_items.UnownedQuestionSource))
            verified: frozenset[int] = handoff.answer_events if isinstance(handoff, review_items.FiledReviewExchange) else frozenset()
            followups: frozenset[int] = handoff.filed_followup_indices if isinstance(handoff, review_items.FiledReviewExchange) else frozenset()
            if (any(_answer_event(event) and index not in verified for index, event in enumerate(item.log))
                    or any(index not in followups for index, _ in enumerate(source.record.item.get("followups", ())))):
                return absent
        case review_items.ReviewType.CHANGE:
            expected = (the_list.ReportKind.CHANGE, the_list.ReportKind.VALIDATION)
        case review_items.ReviewType.PROPOSAL:
            expected = (the_list.ReportKind.PROPOSAL,)
        case review_items.ReviewType.EFFICIENCY:
            expected = (the_list.ReportKind.EFFICIENCY,)
        case _:
            return absent
    matches = the_list.unique_destinations(
        investigation.ListDestination(entry.keeper, entry.item.number)
        for entry in candidates if isinstance(report := entry.item.report, the_list.ListReport)
        and report.kind in expected and marker == f"moved to List item {entry.item.number}")
    return matches[0] if len(matches) == 1 else absent


def apply_review_handoffs(items: list[review_items.ReviewItem], zone: ZoneInfo, *,
                          sources: list[Path] | None = None,
                          members: staff.StaffSnapshot | None = None) -> list[review_items.ReviewItem]:
    """Verify moved reports and exchange receipts before hiding Review cards."""
    keys = {item.key: _review_source_key(item) for item in items if item.type in _REVIEW_SOURCE_KINDS}
    if not keys:
        return items
    members = staff.snapshot() if members is None else members
    by_key = set(keys.values())
    index: dict[str, list[list_page.ListRecord]] = defaultdict(list)
    reports: dict[str, list[list_page.ListRecord]] = defaultdict(list)
    for entry in list_page.records(include_closed=True, sources=sources):
        for key in {exchange.source_key for exchange in entry.item.filed_exchanges} & by_key:
            index[key].append(entry)
        if isinstance(report := entry.item.report, the_list.ListReport):
            reports[report.evidence_key].append(entry)
            if report.submission_scope:
                reports[report.submission_scope].append(entry)
    projected: list[review_items.ReviewItem] = []
    for item in items:
        handoff = review_items.NoFiledReviewExchange.ABSENT
        key = keys.get(item.key, "")
        filed = index.get(key, ())
        if filed:
            with review_items.after_store_write(item.key):
                saved_history: list[list_exchange.FiledExchange] = []
                destinations: list[tuple[investigation.ListDestination, tuple[list_exchange.FiledExchange, ...]]] = []
                for entry in filed:
                    destination = investigation.ListDestination(entry.keeper, entry.item.number)
                    saved = tuple(exchange for exchange in entry.item.filed_exchanges if exchange.source_key == key)
                    saved_history.extend(saved)
                    with review_items.guard_source(review_items.source_path(item), sources, entry=item.key,
                                                   skip=(OSError, ValueError)):
                        confirmed = the_list.confirmed_exchanges(key, destination, saved, sources=sources)
                        if confirmed:
                            destinations.append((destination, tuple(exchange for exchange in saved if exchange in confirmed)))
                if len(destinations) == 1:
                    destination, confirmed_rows = destinations[0]
                    with review_items.guard_source(review_items.source_path(item), sources, entry=item.key,
                                                   skip=(OSError, ValueError)):
                        pending = the_list.pending_exchanges(key, sources=sources)
                        history = the_list.combine_exchange_history(tuple(saved_history), pending)
                        verifier = ExchangeVerifier(item, zone, members, history)
                        handoff = verifier.review_handoff(item, key, destination, confirmed_rows)
        if review_items.pending_list_move(item) is investigation.NoListMove.ABSENT:
            candidates = reports.get(item.key, ())
            if isinstance(source := item.source, review_items.InvestigationSource) and source.record.submission_scope:
                candidates = (*candidates, *reports.get(source.record.submission_scope, ()))
            moved = _moved_report(item, candidates, handoff, sources)
        else:
            moved = investigation.NoListDestination.ABSENT
            if isinstance(handoff, review_items.FiledReviewExchange):
                handoff = replace(handoff, hides_card=False)
        projected.append(replace(item, exchange_handoff=handoff, moved_report=moved)
                         if handoff != item.exchange_handoff or moved != item.moved_report else item)
    return projected


def _unresolved_lines(inventory: ExchangeInventory) -> Iterator[str]:
    yield f"Unresolved legacy replies: {sum(count for _, count in inventory.unresolved)}"
    for key, count in inventory.unresolved:
        yield f"  {key}: {count}"


def unresolved_question_lines(keys: Iterable[str], keyless: Iterable[KeylessQuestionMigration]) -> Iterator[str]:
    unresolved = set(keys)
    rows: list[str] = []
    for migration in keyless:
        location = migration.destination
        identity = f"{location.keeper}/L{location.number}"
        rows.append(identity)
        unresolved.update(origin.source_key for origin in migration.row.candidate_origins)
        if not migration.row.candidate_origins:
            unresolved.add(identity)
    yield f"Unresolved question destinations: {len(unresolved)}"
    for key in sorted(unresolved):
        yield f"  {key}: no verified List destination"
    for row in sorted(rows):
        yield f"  {row}: cannot resolve the original question source"


def migrate(inventory: ExchangeInventory, members: staff.StaffSnapshot, *, apply: bool) -> int:
    if apply:
        try:
            _ = _migration_list_records()
        except the_list.IncompleteListScan as error:
            print(f"List migration: {error}", file=sys.stderr)
            return 1
    groups: dict[tuple[str, investigation.ListDestination], list[list_exchange.FiledExchange]] = defaultdict(list)
    for destination, exchange in inventory.filed:
        groups[exchange.source_key, destination].append(exchange)
    saved = {exchange for (key, destination), group in groups.items()
             for exchange in the_list.confirmed_exchanges(key, destination, group)}
    failures = 0
    unresolved: set[str] = set()
    unresolved_rows: list[KeylessQuestionMigration] = []
    for migration in inventory.keyless:
        destination = migration.destination
        if not apply:
            if migration.origin is the_list.NoQuestionItem.ORDINARY:
                unresolved_rows.append(migration)
            continue
        try:
            with record_store.list_destination_lock():
                _, links = _migration_list_records()
                resolve = partial(_keyless_question_origin, destination, links, inventory.question_records)
                if the_list.convert_legacy_question_source(destination.keeper, f"L{destination.number}", resolve):
                    continue
        except (OSError, ValueError) as error:
            failures += 1
            print(f"List question conversion for {destination.keeper}/L{destination.number}: {error}", file=sys.stderr)
        unresolved_rows.append(migration)
    blocked_rows = {the_list.destination_key(migration.destination) for migration in unresolved_rows}
    blocked_keys = {origin.source_key for migration in unresolved_rows for origin in migration.row.candidate_origins}
    for source in Source:
        plans: list[FilingPlan] = []
        for plan in _plans((exchange for exchange in inventory.exchanges if exchange.source is source), inventory.filed):
            key = plan.exchanges[0].source_key
            if (plan.unresolved_question_destination or key in blocked_keys
                    or any(_link_key(destination) in blocked_rows for destination in plan.destinations)):
                unresolved.add(key)
            else:
                plans.append(plan)
        pending = tuple(plan for plan in plans if not all(list_exchange.FiledExchange.from_exchange(exchange) in saved for exchange in plan.exchanges))
        print(f"{source}: sources={len(pending)} exchanges={sum(len(plan.exchanges) for plan in pending)} "
              + f"linked={sum(bool(plan.destinations) for plan in pending)} "
              + f"new-rows={sum(not plan.destinations for plan in pending)} unchanged={len(plans) - len(pending)}")
        if not apply:
            continue
        for plan in pending:
            key = plan.exchanges[0].source_key
            try:
                link = file_source(plan, members, inventory.reload[key], question_filing=the_list.QuestionFiling.HISTORY)
            except (OSError, ValueError) as error:
                failures += 1
                print(f"  {key}: {error}", file=sys.stderr)
            else:
                if isinstance(link, investigation.ListDestination):
                    print(f"  {key} -> {link.keeper}/L{link.number}")
                elif link is NoExchangeFiling.UNRESOLVED_QUESTION:
                    unresolved.add(key)
    if apply:
        try:
            with record_store.list_destination_lock():
                entries, links = _migration_list_records()
                keyless_paths = {the_list.destination_key(destination) for destination in links.keyless}
                for entry in entries:
                    item = entry.item
                    destination = entry.destination
                    if the_list.destination_key(destination) in keyless_paths:
                        continue
                    origin = item.native_question
                    if origin is the_list.NoQuestionItem.ORDINARY and item.report is the_list.NoListReport.ABSENT:
                        origin = _resolved_question_origin(destination, links, inventory.question_records,
                                                           the_list.native_question_origins(item.filed_exchanges))
                    if the_list.project_question_migration(item, submission_question=origin) != item:
                        try:
                            the_list.migrate_question_item(entry.keeper, item.id, origin)
                        except (OSError, ValueError) as error:
                            failures += 1
                            print(f"  {entry.keeper}/{item.id}: {error}", file=sys.stderr)
        except the_list.IncompleteListScan as error:
            failures += 1
            print(f"List migration: {error}", file=sys.stderr)
    for line in _unresolved_lines(inventory):
        print(line)
    for line in unresolved_question_lines(unresolved, unresolved_rows):
        print(line)
    return int(bool(failures))


class MigrationArguments(argparse.Namespace):
    command: str = ""
    apply: bool = False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("command", choices=("count", "migrate"))
    _ = parser.add_argument("--apply", action="store_true", help="Save the migration to the List.")
    args = parser.parse_args(namespace=MigrationArguments())
    if args.apply and args.command != "migrate":
        parser.error("--apply requires migrate")
    members = staff.snapshot()
    root = change_routing.review_root()
    zone = user_zone()
    reviews = review_items.read(root, members=members, kinds=_REVIEW_SOURCE_KINDS, zone=zone,
                                sources=[], include_filed=True)
    try:
        inventory = _read_exchanges(root, members, zone, reviews)
    except the_list.IncompleteListScan as error:
        print(f"List inventory: {error}", file=sys.stderr)
        return 1
    if args.command == "migrate":
        return migrate(inventory, members, apply=args.apply)
    for line in _count(inventory):
        print(line)
    if inventory.unresolved:
        for line in _unresolved_lines(inventory):
            print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

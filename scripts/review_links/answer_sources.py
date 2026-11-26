"""Read the saved questions and answers that belong on the Studio List."""
from __future__ import annotations

import argparse
import hashlib
import sys
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from functools import partial
from pathlib import Path
from typing import final
from zoneinfo import ZoneInfo

from scripts.lib.user_zone import user_zone
from scripts.production import change_routing, delivery_state, investigation, list_exchange, proposal_issue, staff, the_list, update_registration
from scripts.production.list_exchange import (
    AnswerRead as AnswerRead, Direction as Direction, ExchangeAnswer as ExchangeAnswer, Nate as Nate,
    QuestionWithoutAnswer as QuestionWithoutAnswer, RecordedAnswerer as RecordedAnswerer, Source as Source,
    TimeNotRecorded as TimeNotRecorded, ExchangeBinding, LoggedReply, QuestionEvent, QuestionOrigin,
    QuestionRecord, QuestionSlot, ReplyOrigin, TaggedReply, UnboundExchange, ReplyWithoutEvent,
)
from scripts.review_links import (
    image_store, images, list_page, new_item_dialog, question_path, question_record, review_items, review_submissions,
)
from scripts.review_links.review_events import ReviewLog


@dataclass(frozen=True)
class LegacyListLink:
    """The link names a number without a unique List keeper."""

    number: int


type ListLink = investigation.ListDestination | LegacyListLink


@dataclass(frozen=True)
class Exchange(list_exchange.Exchange):
    list_links: tuple[ListLink, ...]


type ReloadExchanges = Callable[[], Iterable[Exchange]]


@dataclass(frozen=True)
class ExchangeInventory:
    exchanges: tuple[Exchange, ...]
    list_items: frozenset[investigation.ListDestination]
    filed: tuple[tuple[investigation.ListDestination, list_exchange.FiledExchange], ...] = ()
    reload: dict[str, ReloadExchanges] = field(default_factory=dict, compare=False)
    unresolved: tuple[tuple[str, int], ...] = ()


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


def _answerer(reference: str, members: staff.StaffSnapshot) -> staff.StaffId | RecordedAnswerer:
    member = members.by_member_reference(reference)
    if member is None:
        matches = [row for row in (*members.rows, *members.retired_producers)
                   if reference in (row.id, row.name)]
        member = matches[0] if len(matches) == 1 else None
    return member.id if member is not None else RecordedAnswerer(reference)


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


def _reply_windows(log: tuple[ReviewLog, ...], requests: Iterable[int], *,
                   include_tagged: bool = False) -> dict[int, tuple[int, ...]]:
    """Find possible replies within each outgoing request's log interval."""
    windows: dict[int, list[int]] = {index: [] for index in requests}
    question = -1
    for index, event in enumerate(log):
        if index in windows:
            question = index
        elif _request_boundary(event):
            question = -1
        elif question >= 0 and (include_tagged or not event.feedback_request_id) and _answer_event(event):
            windows[question].append(index)
    return {index: tuple(positions) for index, positions in windows.items()}


class Verification(StrEnum):
    VERIFIED = "the evidence agrees"
    CONFLICT = "the evidence disagrees"
    MISSING = "the evidence cannot establish ownership"


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


@dataclass(frozen=True)
class _VerifiedReply:
    position: int
    request: RequestInterval


@dataclass(frozen=True)
class ReplyEvidence:
    text: str = ""
    time: datetime | TimeNotRecorded = TimeNotRecorded.ABSENT
    answerer: staff.StaffId | RecordedAnswerer | Nate | _AnswererEvidence = _AnswererEvidence.NOT_RECORDED
    locator: int | None = None
    binding: ReplyOrigin = ReplyWithoutEvent.NATIVE_RECORD


@dataclass(frozen=True)
class OpenQuestionSlot:
    tag: str
    text: str


@dataclass(frozen=True)
class AnsweredQuestionSlot:
    tag: str
    text: str
    reply: ReplyEvidence


class NoQuestionSlot(StrEnum):
    ABSENT = "no question slot"


@dataclass(frozen=True)
class ReplyIdentity:
    author: staff.StaffId | RecordedAnswerer
    time: datetime
    words: str


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
        self.slot = _change_slot(source if native else source.source, members)
        native_pairs = source.item.get("followups", []) if native else []
        self.prompts = _question_prompts(log)
        self.windows = _reply_windows(log, self.prompts, include_tagged=True)
        self.answers = tuple(index for index, event in enumerate(log) if _answer_event(event))
        self.boundary = _first_request_boundary(log)
        self.claimed: dict[int, str] = {}
        self.claimed_digests: set[str] = set()
        self.verified_handoff_ids: set[str] = set()
        self.terminal = tuple(index for index, event in enumerate(log) if _terminal_boundary(event))
        self.digests = {index: event_digest(log[index]) for index in self.answers}
        self.identities = {index: ReplyIdentity(_answerer(log[index].who, members), log[index].time,
                                               log[index].words[8:].rstrip()) for index in self.answers}
        self.saved_digests: dict[str, set[str]] = defaultdict(set)
        self.saved_tags: dict[str, set[str]] = defaultdict(set)
        self.saved_slots: dict[str, set[str]] = defaultdict(set)
        self.saved_identities: dict[ReplyIdentity, set[str]] = defaultdict(set)
        self.disputed: set[int] = set()
        self.bound: set[int] = set()
        self.tags: dict[str, list[int]] = defaultdict(list)
        self.tagged_replies: dict[str, list[int]] = defaultdict(list)
        self.native_tags: dict[str, list[str]] = defaultdict(list)
        for index, event in enumerate(log):
            tag = event.feedback_request_id
            if tag:
                self.tags[tag].append(index)
                if index in self.digests:
                    self.tagged_replies[tag.removeprefix("answer:")].append(index)
        for reply in native_pairs:
            if tag := reply.get("question_id"):
                self.native_tags[tag].append(reply["followup"])
        for saved in history:
            if not isinstance(saved.answer, ExchangeAnswer):
                continue
            binding = saved.binding
            if isinstance(binding, ExchangeBinding):
                if isinstance(binding.reply, LoggedReply):
                    self.saved_digests[binding.reply.digest].add(saved.exchange_id)
                elif isinstance(binding.reply, TaggedReply):
                    self.saved_tags[binding.reply.tag].add(saved.exchange_id)
                if binding.slot_id:
                    self.saved_slots[binding.slot_id].add(saved.exchange_id)
            if saved.exchange_id.startswith("question:"):
                self.saved_slots[saved.exchange_id.removeprefix("question:")].add(saved.exchange_id)
            if isinstance(saved.answer.time, datetime) and (
                    binding is UnboundExchange.LEGACY or binding.reply is ReplyWithoutEvent.SLOT):
                author = saved.answerer if not isinstance(saved.answerer, Nate) else RecordedAnswerer(saved.answerer)
                identity = ReplyIdentity(author, saved.answer.time, saved.answer.text.rstrip())
                self.saved_identities[identity].add(saved.exchange_id)

    def _source_author(self, identity: str, prompt: str) -> staff.StaffId | _AnswererEvidence:
        if self.report is not None and (identity == "initial" or self.legacy_bug):
            return self.report.owner
        if isinstance(record := self.source, update_registration.QuestionRecord):
            if identity == "legacy-followup":
                return record.asker
            fields = record.item
            if (identity == "followup:1" and "review_submission" in fields
                    and _same_prompt(fields.get("question", fields["text"]), prompt)):
                return record.owner
        return _AnswererEvidence.RESOLVED_STAFF

    def _available(self, position: int, identity: str) -> bool:
        digest = self.digests[position]
        event = self.log[position]
        return (position not in self.claimed and identity not in self.verified_handoff_ids
                and digest not in self.claimed_digests
                and not (self.saved_digests.get(digest, set()) - {identity})
                and not (self.saved_tags.get(event.feedback_request_id, set()) - {identity})
                and not (self.saved_identities.get(self.identities[position], set()) - {identity}))

    def _claim(self, position: int, request: RequestInterval, identity: str) -> None:
        self.claimed[position] = identity
        self.verified_handoff_ids.add(identity)
        self.claimed_digests.add(self.digests[position])
        if isinstance(request.start, int):
            self.bound.add(request.start)

    def _native_request(self, text: str, tag: str, proof: ReplyProof, *,
                       initial: bool) -> RequestInterval:
        matches = [start for start, prompt in self.prompts.items() if _same_prompt(prompt.text, text)]
        tagged = [start for start in matches if tag and self.log[start].feedback_request_id == tag]
        if len(tagged) == 1 and (not initial or (tagged[0] == self.boundary and proof is not ReplyProof.INTERVAL)):
            return RequestInterval(text, tagged[0])
        if initial:
            return RequestInterval(text, _SyntheticRequest.INITIAL)
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
        requests = [index for index in self.tags[tag] if index not in self.digests]
        if requests and (requests != [request.start] or request.start not in self.prompts
                         or not _same_prompt(self.prompts[requests[0]].text, request.text)):
            return False
        answers = self.tagged_replies[tag]
        return (not log_tag or log_tag in (tag, question_path.answer_log_id(tag))) and (
            not answers or answers == [position]) and all(
                index == position for index in self.tags[question_path.answer_log_id(tag)])

    def _reply_agrees(self, position: int, evidence: ReplyEvidence) -> bool:
        event = self.log[position]
        if evidence.text and event.words[8:].rstrip() != evidence.text.rstrip():
            return False
        if isinstance(evidence.time, datetime) and event.time != evidence.time:
            return False
        if isinstance(evidence.binding, (LoggedReply, TaggedReply)):
            if _event_position(evidence.binding, self.log) != position:
                return False
        if evidence.locator is not None and evidence.locator != position and (
                not isinstance(evidence.binding, LoggedReply) or evidence.locator in self.digests):
            return False
        return self._author_agrees(position, evidence.answerer)

    def _author_agrees(self, position: int, author: staff.StaffId | RecordedAnswerer | Nate | _AnswererEvidence) -> bool:
        if author is _AnswererEvidence.NOT_RECORDED:
            return True
        resolved = self.identities[position].author
        if author is _AnswererEvidence.RESOLVED_STAFF:
            return not isinstance(resolved, RecordedAnswerer)
        return self.log[position].who == author or resolved == author

    def _competing(self, start: int) -> bool:
        return any(index not in self.bound and event.who == Nate.USER
                   and event.words.partition(": ")[0].lower() in ("question", "followup", "feedback")
                   for index, event in enumerate(self.log[:start]))

    def _verify(self, request: RequestInterval, position: int, proof: ReplyProof, identity: str, *, tag: str = "",
                evidence: ReplyEvidence, saved_slot: str = "", historical: bool = False) -> Verification:
        if (position not in self.digests or position in self.disputed or not self._available(position, identity)
                or not self._tags_agree(request, position, tag) or not self._reply_agrees(position, evidence)):
            return Verification.CONFLICT
        owner = self._source_author(identity, request.text)
        if not self._author_agrees(position, owner):
            return Verification.CONFLICT
        start, event = request.start, self.log[position]
        tagged = bool(event.feedback_request_id)
        beginning = start if isinstance(start, int) else -1
        if any(beginning < index < position for index in self.terminal):
            return Verification.CONFLICT
        if isinstance(start, int):
            if position <= start or (position not in self.windows[start] and not tagged):
                return Verification.CONFLICT
        elif start is _SyntheticRequest.INITIAL:
            if position >= self.boundary:
                return Verification.CONFLICT
        elif start is _SyntheticRequest.NATIVE_PAIR:
            if position >= self.boundary:
                return Verification.CONFLICT
        elif start is _SyntheticRequest.UNLOCATED or not tagged:
            return Verification.MISSING

        slot = self.slot
        if isinstance(start, int):
            tag = tag or self.log[start].feedback_request_id
        if self.saved_slots.get(tag or saved_slot, set()) - {identity}:
            return Verification.CONFLICT
        if not isinstance(slot, NoQuestionSlot) and (slot.tag in (tag, saved_slot)
                or (not historical and not tag and _same_prompt(request.text, slot.text))):
            if (isinstance(slot, OpenQuestionSlot) or not _same_prompt(request.text, slot.text)
                    or not self._reply_agrees(position, slot.reply)):
                return Verification.CONFLICT
            if not tag and not tagged:
                if not isinstance(slot.reply.time, datetime) and not isinstance(evidence.time, datetime):
                    return Verification.MISSING
                if sum(_same_prompt(prompt.text, slot.text) for prompt in self.prompts.values()) != 1:
                    return Verification.MISSING

        if proof is ReplyProof.SAVED and not isinstance(evidence.binding, (LoggedReply, TaggedReply)):
            return Verification.MISSING
        if proof is ReplyProof.LOCATOR and evidence.locator is None:
            return Verification.MISSING
        if proof is ReplyProof.TAG and not tagged:
            return Verification.MISSING
        if proof is ReplyProof.TIME and not isinstance(evidence.time, datetime):
            return Verification.MISSING
        if proof is ReplyProof.INTERVAL:
            if start is _SyntheticRequest.INITIAL:
                if sum(index < self.boundary for index in self.answers) != 1:
                    return Verification.MISSING
            elif isinstance(start, int):
                if len(self.windows[start]) != 1 or self._competing(start):
                    return Verification.MISSING
            else:
                return Verification.MISSING
        return Verification.VERIFIED

    def native_exchanges(self, key: str, links: tuple[ListLink, ...],
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
            prompt, position = derived
            event = log[position]
            answer = ExchangeAnswer(event.words[8:], event.time, AnswerRead.READ)
            yield Exchange(source, key, "legacy-followup", Direction.ANSWER_TO_NATE, prompt.text,
                           prompt.time, Nate.USER, record.asker, answer, links,
                           binding=ExchangeBinding(prompt.origin, _reply_origin(position, event)))
        if unresolved is not None:
            claimed = {verified.position for verified in positions.values()}
            if derived is not None:
                claimed.add(derived[1])
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
            binding = ExchangeBinding(QuestionRecord(identity),
                                      _reply_origin(position, log[position]) if position is not None else ReplyWithoutEvent.NATIVE_RECORD)
            self.verified_handoff_ids.add(identity)
            yield Exchange(source, key, identity, Direction.ANSWER_TO_NATE, reply["followup"],
                           asked_at, Nate.USER, author, answer, links, binding=binding)

    def _native_positions(self) -> dict[int, _VerifiedReply]:
        record = self.source
        assert isinstance(record, update_registration.QuestionRecord)
        fields = record.item
        replies = fields.get("followups", [])
        locators = fields.get("reply_log_positions", {})
        positions: dict[int, _VerifiedReply] = {}

        def candidate(index: int, position: int, strength: ReplyProof) -> _VerifiedReply | None:
            reply = replies[index]
            tag, text = reply.get("question_id", ""), reply["followup"]
            identity = f"followup:{index + 1}"
            saved = self.filed.get(identity)
            binding = saved.binding if saved is not None else UnboundExchange.LEGACY
            origin = binding.reply if isinstance(binding, ExchangeBinding) else ReplyWithoutEvent.NATIVE_RECORD
            stamp = reply.get("replied_at") or (fields.get("answered_at") if index == len(replies) - 1 else None)
            time = review_items.moment(stamp, self.zone) if stamp is not None else TimeNotRecorded.ABSENT
            initial = (index == 0 and "review_submission" in fields
                       and _same_prompt(fields.get("question", fields["text"]), text))
            request = self._native_request(text, tag, strength, initial=initial)
            author: staff.StaffId | RecordedAnswerer | Nate | _AnswererEvidence = _AnswererEvidence.NOT_RECORDED
            if saved is not None and isinstance(origin, (LoggedReply, TaggedReply)):
                if saved.question != text or not isinstance(saved.answer, ExchangeAnswer) or saved.answer.text != reply["reply"]:
                    return None
                author = saved.answerer
                if isinstance(saved.answer.time, datetime):
                    if isinstance(time, datetime) and time != saved.answer.time:
                        return None
                    time = saved.answer.time
            evidence = ReplyEvidence(reply["reply"], time, author, locators.get(str(index)), origin)
            if self._verify(request, position, strength, identity, tag=tag, evidence=evidence) is Verification.VERIFIED:
                return _VerifiedReply(position, request)
            return None

        for strength in (ReplyProof.SAVED, ReplyProof.LOCATOR, ReplyProof.TAG, ReplyProof.TIME):
            candidates: dict[int, list[tuple[int, _VerifiedReply]]] = defaultdict(list)
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
                    if (verified := candidate(index, position, strength)) is not None:
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
                identity = f"followup:{index + 1}"
                if self._available(position, identity):
                    positions[index] = verified
                    self._claim(position, verified.request, identity)

        for position in self.answers:
            matches = [(index, verified) for index in range(len(replies)) if index not in positions
                       and (verified := candidate(index, position, ReplyProof.INTERVAL)) is not None]
            if len(matches) == 1:
                index, verified = matches[0]
                if all((other < index) == (known.position < position) for other, known in positions.items()):
                    positions[index] = verified
                    self._claim(position, verified.request, f"followup:{index + 1}")
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

    def _staff_log_reply(self) -> tuple[_Prompt, int] | None:
        record = self.source
        assert isinstance(record, update_registration.QuestionRecord)
        fields, asker = record.item, record.asker
        if len(self.prompts) != 1:
            return None
        question, prompt = next(iter(self.prompts.items()))
        if not _same_prompt(prompt.text, fields.get("waiting_why", "")):
            return None
        saved = self.filed.get("legacy-followup")
        evidence = ReplyEvidence()
        if saved is not None:
            if (saved.question != prompt.text or saved.asked_at != prompt.time or saved.answerer != asker
                    or not isinstance(saved.answer, ExchangeAnswer)):
                return None
            binding = saved.binding.reply if isinstance(saved.binding, ExchangeBinding) else evidence.binding
            evidence = ReplyEvidence(saved.answer.text, saved.answer.time, saved.answerer, binding=binding)
        request = RequestInterval(prompt.text, question)
        for position in self.windows[question]:
            proof = ReplyProof.SAVED if isinstance(evidence.binding, (LoggedReply, TaggedReply)) else ReplyProof.INTERVAL
            if self._verify(request, position, proof, "legacy-followup", evidence=evidence) is Verification.VERIFIED:
                self._claim(position, request, "legacy-followup")
                return prompt, position
        return None

    def _saved_reply_request(self, saved: list_exchange.Exchange, position: int) -> RequestInterval | None:
        binding, answer = saved.binding, saved.answer
        if not isinstance(binding, ExchangeBinding) or not isinstance(answer, ExchangeAnswer):
            return None
        origin = binding.question
        if isinstance(origin, QuestionEvent):
            prompt = _bound_event_prompt(origin, saved, position, self.prompts, self.windows)
            if prompt is None or not isinstance(prompt.origin, QuestionEvent):
                return None
            start = prompt.origin.position
        elif isinstance(origin, QuestionSlot):
            starts = [index for index, prompt in self.prompts.items() if prompt.origin == origin]
            if len(starts) > 1:
                return None
            start = starts[0] if starts else _SyntheticRequest.SLOT
        else:
            if saved.exchange_id != "initial" or self.report is None or saved.question != self.report.question:
                return None
            start = _SyntheticRequest.INITIAL
        request = RequestInterval(saved.question, start)
        tag = origin.identity if isinstance(origin, QuestionSlot) else ""
        evidence = ReplyEvidence(answer.text, answer.time, saved.answerer, binding=binding.reply)
        if self._verify(request, position, ReplyProof.SAVED, saved.exchange_id, tag=tag, evidence=evidence,
                        saved_slot=binding.slot_id, historical=True) is Verification.VERIFIED:
            return request
        return None

    def _legacy_matches(self, saved: list_exchange.FiledExchange) -> Iterator[tuple[_Prompt, _VerifiedReply]]:
        answer = saved.answer
        if not isinstance(answer, ExchangeAnswer):
            return
        old_slot = saved.exchange_id.removeprefix("question:") if saved.exchange_id.startswith("question:") else ""
        if not old_slot and not isinstance(answer.time, datetime):
            return
        for start, prompt in self.prompts.items():
            if prompt.text != saved.question:
                continue
            if isinstance(prompt.origin, QuestionSlot):
                if old_slot and old_slot != prompt.origin.identity:
                    continue
            elif old_slot:
                if not isinstance(answer.time, datetime):
                    continue
                prompt = replace(prompt, origin=QuestionEvent(start, old_slot))
            elif not isinstance(saved.asked_at, datetime) or prompt.time != saved.asked_at:
                continue
            request = RequestInterval(prompt.text, start)
            evidence = ReplyEvidence(answer.text, answer.time, saved.answerer)
            tag = self.log[start].feedback_request_id
            positions = self.tagged_replies.get(tag, ()) if tag else ()
            proof = ReplyProof.TAG if positions else ReplyProof.INTERVAL
            if not positions:
                positions = self.windows[start]
            for position in positions:
                if (not isinstance(answer.time, datetime) and not tag
                        or self._verify(request, position, proof, saved.exchange_id, tag=tag, evidence=evidence, saved_slot=old_slot)
                        is not Verification.VERIFIED):
                    continue
                yield prompt, _VerifiedReply(position, request)

    def _verify_new_reply(self, request: RequestInterval, prompt: _Prompt, position: int, proof: ReplyProof,
                          tag: str, evidence: ReplyEvidence, accepted: dict[str, Exchange],
                          unbound: list[list_exchange.FiledExchange]) -> Verification:
        event = self.log[position]
        identity = _new_event_id(prompt, event)
        if identity in accepted or any(saved.question == prompt.text and isinstance(saved.answer, ExchangeAnswer)
                and saved.answer.text == event.words[8:]
                and (not isinstance(saved.answer.time, datetime) or saved.answer.time == event.time)
                for saved in unbound):
            return Verification.CONFLICT
        return self._verify(request, position, proof, identity, tag=tag, evidence=evidence)

    def _verify_initial(self, request: RequestInterval, position: int, record: investigation.Investigation,
                        saved: list_exchange.FiledExchange | None) -> Verification:
        event = self.log[position]
        if self.legacy_bug and event.words[8:] != record.answer:
            last = self.answers[-1]
            if last not in self.claimed or self.log[last].words[8:] != record.answer:
                return Verification.CONFLICT
        binding: ReplyOrigin = ReplyWithoutEvent.NATIVE_RECORD
        if saved is not None and isinstance(saved.answer, ExchangeAnswer):
            if (saved.question != request.text or saved.answerer != record.owner or saved.answer.text != event.words[8:]
                    or isinstance(saved.answer.time, datetime) and saved.answer.time != event.time):
                return Verification.CONFLICT
            if isinstance(saved.binding, ExchangeBinding):
                binding = saved.binding.reply
        proof = ReplyProof.SAVED if isinstance(binding, (TaggedReply, LoggedReply)) else ReplyProof.INTERVAL
        return self._verify(request, position, proof, "initial",
                            evidence=ReplyEvidence(binding=binding))

    @staticmethod
    def _verify_native_initial(record: investigation.Investigation, time: datetime,
                               saved: list_exchange.FiledExchange | None) -> Verification:
        if saved is None:
            return Verification.VERIFIED
        binding, answer = saved.binding, saved.answer
        if (saved.question != record.question or saved.answerer != record.owner
                or not isinstance(answer, ExchangeAnswer) or answer.text != record.answer
                or isinstance(answer.time, datetime) and answer.time != time
                or isinstance(binding, ExchangeBinding) and binding.reply is not ReplyWithoutEvent.NATIVE_RECORD):
            return Verification.CONFLICT
        return Verification.VERIFIED

    def scoped_exchanges(self, key: str, links: tuple[ListLink, ...],
                         unresolved: list[tuple[str, int]] | None) -> Iterator[Exchange]:
        """Project accepted exchanges and retain the verified claims for Review."""
        item = self.source
        assert isinstance(item, review_items.ReviewItem)
        source = item.source
        log, members, zone = self.log, self.members, self.zone
        slot = self.slot
        history = self.history
        read_times = tuple(_read_times(log))
        acknowledged = item.state is review_items.ReviewState.DONE
        record = self.report
        if record is not None and record.read:
            read_times += (review_items.moment(record.read, zone),)
        legacy_bug = self.legacy_bug

        def exchange(prompt: _Prompt, author: staff.StaffId | RecordedAnswerer, answer: ExchangeAnswer,
                     reply: ReplyOrigin) -> Exchange:
            return Exchange(Source.SCOPED_REPLY, key, prompt.identity, Direction.ANSWER_TO_NATE,
                            prompt.text, prompt.time, Nate.USER, author, answer, links,
                            binding=ExchangeBinding(prompt.origin, reply))

        def from_event(prompt: _Prompt, position: int) -> Exchange:
            event = log[position]
            state = _read_state(event.time, read_times, acknowledged=acknowledged)
            return exchange(replace(prompt, identity=_new_event_id(prompt, event)), _answerer(event.who, members),
                            ExchangeAnswer(event.words[8:], event.time, state), _reply_origin(position, event))

        accepted: dict[str, Exchange] = {}
        bound_slots: set[str] = set()

        def keep(value: Exchange) -> None:
            accepted[value.exchange_id] = value
            if isinstance(value.binding, ExchangeBinding) and value.binding.slot_id:
                bound_slots.add(value.binding.slot_id)

        legacy = [saved for saved in history if saved.binding is UnboundExchange.LEGACY and saved.exchange_id != "initial"]
        saved_initial = self.filed.get("initial")
        prior_claims: dict[int, list[tuple[str, RequestInterval]]] = defaultdict(list)
        for saved in history:
            if saved in legacy or saved is saved_initial:
                continue
            keep(_saved_scoped_exchange(saved, links, read_times, acknowledged))
            if isinstance(saved.binding, ExchangeBinding):
                position = _event_position(saved.binding.reply, log)
                if position is not None:
                    request = self._saved_reply_request(saved, position)
                    if request is None:
                        self.disputed.add(position)
                    else:
                        prior_claims[position].append((saved.exchange_id, request))
        for position, claims in prior_claims.items():
            if len(claims) == 1 and position not in self.disputed:
                identity, request = claims[0]
                if self._available(position, identity):
                    self._claim(position, request, identity)
                else:
                    self.disputed.add(position)
            else:
                self.disputed.add(position)

        upgrades: dict[int, list[tuple[list_exchange.FiledExchange, _Prompt, RequestInterval]]] = defaultdict(list)
        for saved in legacy:
            matches = tuple(self._legacy_matches(saved))
            if len(matches) == 1:
                prompt, match = matches[0]
                if not isinstance(slot, NoQuestionSlot) and saved.exchange_id == f"question:{slot.tag}" and isinstance(prompt.origin, QuestionEvent):
                    prompt = replace(prompt, origin=QuestionEvent(prompt.origin.position, slot.tag))
                upgrades[match.position].append((saved, prompt, match.request))
        upgraded: set[str] = set()
        for position, matches in upgrades.items():
            if len(matches) != 1 or not self._available(position, matches[0][0].exchange_id):
                self.disputed.add(position)
                continue
            saved, prompt, request = matches[0]
            keep(replace(from_event(prompt, position), exchange_id=saved.exchange_id))
            self._claim(position, request, saved.exchange_id)
            upgraded.add(saved.exchange_id)
        unbound = [saved for saved in legacy if saved.exchange_id not in upgraded]
        for saved in unbound:
            keep(_saved_scoped_exchange(saved, links, read_times, acknowledged))

        verified: dict[int, Exchange] = {}
        for start, prompt in self.prompts.items():
            if start in self.bound:
                continue
            tag = self.log[start].feedback_request_id
            if tag in bound_slots or (not isinstance(slot, NoQuestionSlot) and not tag and slot.tag in bound_slots
                                      and _same_prompt(slot.text, prompt.text)):
                continue
            tagged = self.tagged_replies.get(tag, ())
            time = slot.reply.time if isinstance(slot, AnsweredQuestionSlot) and tag == slot.tag else TimeNotRecorded.ABSENT
            proof = ReplyProof.TAG if tagged else ReplyProof.TIME if isinstance(time, datetime) else ReplyProof.INTERVAL
            request = RequestInterval(prompt.text, start)
            evidence = ReplyEvidence(time=time)
            candidates = [position for position in tagged or self.windows[start]
                          if self._verify_new_reply(request, prompt, position, proof, tag, evidence, accepted, unbound)
                          is Verification.VERIFIED]
            if len(candidates) != 1:
                continue
            position = candidates[0]
            if not isinstance(slot, NoQuestionSlot) and not tag and _same_prompt(slot.text, prompt.text):
                prompt = replace(prompt, origin=QuestionEvent(start, slot.tag))
            value = from_event(prompt, position)
            verified[position] = value
            self._claim(position, request, value.exchange_id)

        initial = None
        suspect = saved_initial.answer if saved_initial is not None and isinstance(saved_initial.answer, ExchangeAnswer) else None
        if record is not None and (legacy_bug or (record.kind is investigation.InvestigationKind.INVESTIGATION
                and record.purpose in (investigation.InvestigationPurpose.INVESTIGATE,
                                       investigation.InvestigationPurpose.LIST_INVESTIGATION))):
            prompt = _Prompt(QuestionRecord("initial"), "initial", record.question, item.times.filed)
            request = RequestInterval(record.question, _SyntheticRequest.INITIAL)
            for position in (index for index in self.answers if index < self.boundary):
                if self._verify_initial(request, position, record, saved_initial) is Verification.VERIFIED:
                    initial = from_event(prompt, position)
                    self._claim(position, request, "initial")
                    break
            if (initial is None and not legacy_bug and not self.answers and self.boundary == len(log)
                    and record.answer and record.answered):
                time = review_items.moment(record.answered, zone)
                if self._verify_native_initial(record, time, saved_initial) is Verification.VERIFIED:
                    state = _read_state(time, read_times, acknowledged=acknowledged)
                    initial = exchange(prompt, record.owner, ExchangeAnswer(record.answer, time, state),
                                       ReplyWithoutEvent.NATIVE_RECORD)
            if initial is not None:
                if (saved_initial is not None and isinstance(saved_initial.binding, ExchangeBinding)
                        and isinstance(saved_initial.binding.reply, (TaggedReply, LoggedReply))):
                    initial = replace(initial, binding=saved_initial.binding)
                keep(initial)
                self.verified_handoff_ids.add("initial")
                suspect = None

        for position, value in verified.items():
            if (legacy_bug and initial is None) or (suspect is not None and log[position].words == "Answer: " + suspect.text):
                _ = self.claimed.pop(position, None)
                self.verified_handoff_ids.discard(value.exchange_id)
            else:
                keep(value)

        if (isinstance(source, review_items.ChangeSource) and isinstance(slot, AnsweredQuestionSlot) and slot.tag not in bound_slots):
            prompt = _slot_prompt(slot, self.prompts, item.times.filed)
            prompt = replace(prompt, text=slot.text)
            reference = source.answered_by or (source.validators[0] if source.validators else "")
            author = _answerer(reference, members) if reference else RecordedAnswerer("Answerer not recorded")
            answer = ExchangeAnswer(slot.reply.text, slot.reply.time,
                                    _read_state(slot.reply.time, read_times, acknowledged=acknowledged))
            for position in self.tagged_replies.get(slot.tag, ()):
                request = RequestInterval(slot.text, _SyntheticRequest.SLOT)
                if self._verify(request, position, ReplyProof.TAG, prompt.identity, tag=slot.tag,
                                evidence=slot.reply) is Verification.VERIFIED:
                    keep(from_event(prompt, position))
                    self._claim(position, request, prompt.identity)
                    break
            if slot.tag not in bound_slots and self._verify_slot_filing(
                    slot, prompt.identity, answer, author) is Verification.VERIFIED:
                keep(exchange(prompt, author, answer, ReplyWithoutEvent.SLOT))

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
                feedback = _completed_feedback_reply(source, feedback_slot, log, self.prompts, members)
                if feedback is not None:
                    possible.discard(feedback)
            count = len(possible - self.claimed.keys())
            if count or suspect is not None:
                unresolved.append((key, count or 1))
        yield from accepted.values()



    def _verify_slot_filing(self, slot: AnsweredQuestionSlot, identity: str, answer: ExchangeAnswer,
                            author: staff.StaffId | RecordedAnswerer | Nate) -> Verification:
        if (self.saved_slots.get(slot.tag, set()) - {identity} or slot.reply.text != answer.text
                or isinstance(slot.reply.time, datetime) and slot.reply.time != answer.time
                or slot.reply.answerer is not _AnswererEvidence.NOT_RECORDED and slot.reply.answerer != author):
            return Verification.CONFLICT
        if isinstance(answer.time, datetime) and not isinstance(author, Nate):
            reply = ReplyIdentity(author, answer.time, answer.text.rstrip())
            if (self.saved_identities.get(reply, set()) - {identity} or any(
                    self.identities[position] == reply and owner != identity for position, owner in self.claimed.items())):
                return Verification.CONFLICT
        return Verification.VERIFIED

    def review_handoff(self, item: review_items.ReviewItem, key: str, destination: investigation.ListDestination,
                       saved: frozenset[list_exchange.FiledExchange]
                       ) -> review_items.FiledReviewExchange | review_items.NoFiledReviewExchange:
        unresolved: list[tuple[str, int]] = []
        native = isinstance(self.source, update_registration.QuestionRecord)
        candidates = self.native_exchanges(key, (), unresolved) if native else self.scoped_exchanges(key, (), unresolved)
        exchanges = tuple(exchange for exchange in candidates if _eligible(exchange))
        card_can_move = native or self.report is not None
        current = {content for exchange in exchanges if (content := self._handoff_content(
            list_exchange.FiledExchange.from_exchange(exchange))) is not None}
        matched = tuple(exchange for exchange in saved if (card_can_move or isinstance(exchange.binding, ExchangeBinding))
                        and self._handoff_content(exchange) in current)
        hides_card = (card_can_move and bool(matched) and len(matched) == len(exchanges)
                      and all(exchange.exchange_id in self.verified_handoff_ids for exchange in matched)
                      and item.state is not review_items.ReviewState.WAITING and not unresolved)
        if not matched:
            return review_items.NoFiledReviewExchange.ABSENT
        events, slot_id = self._handoff_reply_events(item, matched)
        verified = tuple(exchange for exchange in matched if exchange.exchange_id in self.verified_handoff_ids)
        ids = frozenset(exchange.exchange_id for exchange in verified if isinstance(exchange.answer, ExchangeAnswer))
        open_staff_question = any(exchange.direction is Direction.QUESTION_TO_NATE
                                  and exchange.answer is QuestionWithoutAnswer.OPEN for exchange in verified)
        filed_investigation_answer = False
        if self.report is not None and self.report.answered:
            if self.answers:
                last = self.answers[-1]
                filed_investigation_answer = (self.log[last].words == "Answer: " + self.report.answer
                    and len(item.log) - len(self.log) + last in events)
            else:
                filed_investigation_answer = "initial" in ids
        return review_items.FiledReviewExchange(destination, hides_card, events, ids, open_staff_question,
                                                slot_id, filed_investigation_answer)

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

    def _handoff_reply_events(self, item: review_items.ReviewItem, saved: Iterable[list_exchange.FiledExchange]
                             ) -> tuple[frozenset[int], str]:
        """Project the verified log positions and the current slot on a receipt."""
        offset = len(item.log) - len(self.log)
        positions: set[int] = set()
        filed_slot_id = ""
        for exchange in saved:
            binding, answer = exchange.binding, exchange.answer
            if not isinstance(binding, ExchangeBinding) or not isinstance(answer, ExchangeAnswer):
                continue
            position = _event_position(binding.reply, self.log)
            verified = position is not None and self.claimed.get(position) == exchange.exchange_id
            if isinstance(binding.reply, (LoggedReply, TaggedReply)) and not verified:
                continue
            slot = self.slot
            if (isinstance(slot, AnsweredQuestionSlot)
                    and (slot.tag, slot.text) == (binding.slot_id, exchange.question)
                    and self._verify_slot_filing(slot, exchange.exchange_id, answer, exchange.answerer) is Verification.VERIFIED):
                filed_slot_id = slot.tag
                self.verified_handoff_ids.add(exchange.exchange_id)
                synthetic_tag = question_path.answer_log_id(slot.tag)
                positions.update(index for index, event in enumerate(item.log[:max(0, offset)])
                                 if event.feedback_request_id == synthetic_tag)
            if verified and position is not None and offset >= 0:
                native, displayed = self.log[position], item.log[offset + position]
                if native == replace(displayed, time=review_items.aware(displayed.time, self.zone)):
                    positions.add(offset + position)
        return frozenset(positions), filed_slot_id


def _change_slot(source: review_items.ReviewSource | update_registration.QuestionRecord,
                 members: staff.StaffSnapshot) -> OpenQuestionSlot | AnsweredQuestionSlot | NoQuestionSlot:
    if not isinstance(source, review_items.ChangeSource):
        return NoQuestionSlot.ABSENT
    slot = source.slots.question
    if not isinstance(slot, (delivery_state.AnsweredQuestion, delivery_state.OpenQuestion)) or isinstance(
            slot, (review_items.CompletedFeedback, review_items.OpenFeedback)):
        return NoQuestionSlot.ABSENT
    if isinstance(slot, delivery_state.OpenQuestion):
        return OpenQuestionSlot(slot.question_id, slot.text)
    author = _answerer(source.answered_by, members) if source.answered_by else _AnswererEvidence.NOT_RECORDED
    time = source.answered_at if isinstance(source.answered_at, datetime) else TimeNotRecorded.ABSENT
    return AnsweredQuestionSlot(slot.question_id, slot.text, ReplyEvidence(slot.answer, time, author))


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
    positions = _reply_windows(log, (*prompts, request), include_tagged=True)[request]
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


def _reload_review(root: Path, item: review_items.ReviewItem, members: staff.StaffSnapshot,
                   links: Iterable[ListLink], zone: ZoneInfo) -> Iterator[Exchange]:
    try:
        current = review_items.read_current(root, item, members, include_filed=True)
    except review_items.ReviewRecordMissing:
        return
    yield from _review_item_exchanges(current, links, zone, members, the_list.exchange_history(_review_source_key(current)))


def _review_source_key(item: review_items.ReviewItem) -> str:
    source = item.source
    origin = review_items.submission_origin(item)
    if isinstance(origin, new_item_dialog.ReviewSubmission):
        kind = ("question" if isinstance(source, (review_items.QuestionSource, review_items.UnownedQuestionSource))
                else "proposal" if isinstance(source, review_items.ProposalSource) else item.type.value.lower())
        return f"submission/{kind}/{origin.request_id}"
    return item.key


def _review_item_exchanges(item: review_items.ReviewItem, linked: Iterable[ListLink],
                           zone: ZoneInfo, members: staff.StaffSnapshot,
                           history: tuple[list_exchange.FiledExchange, ...] = (),
                           unresolved: list[tuple[str, int]] | None = None) -> Iterator[Exchange]:
    source = item.source
    destinations = list(linked)
    if (isinstance(source, review_items.InvestigationSource)
            and isinstance(destination := source.record.list_destination, investigation.ListDestination)
            and destination not in destinations):
        destinations.append(destination)
    key = _review_source_key(item)
    if isinstance(source, (review_items.QuestionSource, review_items.UnownedQuestionSource)):
        yield from _native_question_exchanges(source.record, key, tuple(destinations), zone, members, history, unresolved)
    else:
        verifier = ExchangeVerifier(item, zone, members, history)
        yield from verifier.scoped_exchanges(key, tuple(destinations), unresolved)


def _native_question_exchanges(record: update_registration.QuestionRecord, key: str, links: tuple[ListLink, ...],
                               zone: ZoneInfo, members: staff.StaffSnapshot,
                               history: tuple[list_exchange.FiledExchange, ...] = (),
                               unresolved: list[tuple[str, int]] | None = None) -> Iterator[Exchange]:
    verifier = ExchangeVerifier(record, zone, members, history)
    yield from verifier.native_exchanges(key, links, unresolved)


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


def _reload_picture(root: Path, identity: str, keepers: dict[int, list[staff.StaffId]],
                    zone: ZoneInfo) -> Iterator[Exchange]:
    try:
        current = image_store.read_record(images.images_root(root) / identity)
    except FileNotFoundError:
        return
    yield from _picture_exchanges(root, keepers, zone, (current,))


def read_exchanges(root: Path, *, members: staff.StaffSnapshot) -> ExchangeInventory:
    """Read one inventory without changing the records or reserving List numbers."""
    zone = user_zone()
    entries = list_page.records(include_closed=True)
    list_items: set[investigation.ListDestination] = set()
    keepers: dict[int, list[staff.StaffId]] = defaultdict(list)
    links: dict[str, list[ListLink]] = defaultdict(list)
    for entry in entries:
        item = entry.item
        destination = investigation.ListDestination(entry.keeper, item.number)
        list_items.add(destination)
        keepers[item.number].append(entry.keeper)
        keys = set(item.reviews)
        if isinstance(item.report, the_list.ListReport):
            keys.add(item.report.evidence_key)
        for key in keys:
            links[key].append(destination)
    filed = tuple((investigation.ListDestination(entry.keeper, entry.item.number), exchange)
                  for entry in entries for exchange in entry.item.filed_exchanges)
    previous: dict[str, list[list_exchange.FiledExchange]] = defaultdict(list)
    for _, exchange in filed:
        previous[exchange.source_key].append(exchange)
    exchanges: list[Exchange] = []
    unresolved: list[tuple[str, int]] = []
    reload: dict[str, ReloadExchanges] = {}
    for item in review_items.read(root, members=members, kinds=_REVIEW_SOURCE_KINDS, zone=zone,
                                 sources=[], include_filed=True):
        linked = tuple(links.get(item.key, ()))
        key = _review_source_key(item)
        history = the_list.combine_exchange_history(tuple(previous.get(key, ())), the_list.pending_exchanges(key))
        candidates = tuple(_review_item_exchanges(item, linked, zone, members, history, unresolved))
        if candidates:
            reload[key] = partial(_reload_review, root, item, members, linked, zone)
            exchanges.extend(candidates)
    for entry in entries:
        candidates = tuple(_list_exchanges(entry, members, zone))
        if candidates:
            reload[candidates[0].source_key] = partial(_reload_list, entry.keeper, entry.item.id, members, zone)
            exchanges.extend(candidates)
    for record in images.records(root):
        candidates = tuple(_picture_exchanges(root, keepers, zone, (record,)))
        if candidates:
            reload[candidates[0].source_key] = partial(_reload_picture, root, record["id"], keepers, zone)
            exchanges.extend(candidates)
    ordered = sorted(exchanges, key=lambda exchange: (exchange.source, exchange.source_key, exchange.exchange_id))
    return ExchangeInventory(tuple(ordered), frozenset(list_items), filed, reload, tuple(sorted(unresolved)))


def _count(inventory: ExchangeInventory) -> Iterator[str]:
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
                elif link not in inventory.list_items:
                    yield f"  Bad List link: {key} -> {link.keeper}/L{link.number} (item missing)"


def _eligible(exchange: Exchange) -> bool:
    return exchange.answer is not QuestionWithoutAnswer.CLOSED


@dataclass(frozen=True)
class FilingPlan:
    exchanges: tuple[Exchange, ...]
    destinations: tuple[ListLink, ...]
    previously_filed: bool


def _plans(exchanges: Iterable[Exchange], filed: Iterable[tuple[investigation.ListDestination, list_exchange.FiledExchange]]
           ) -> Iterator[FilingPlan]:
    existing: dict[str, set[investigation.ListDestination]] = defaultdict(set)
    for destination, exchange in filed:
        existing[exchange.source_key].add(destination)
    groups: dict[str, list[Exchange]] = defaultdict(list)
    for exchange in exchanges:
        if _eligible(exchange) or exchange.source_key in existing:
            groups[exchange.source_key].append(exchange)
    for key, group in groups.items():
        links = tuple(dict.fromkeys(link for exchange in group for link in exchange.list_links))
        destinations = tuple(dict.fromkeys((*links, *existing[key])))
        yield FilingPlan(tuple(group), destinations, bool(existing[key]))


class NoExchangeFiling(StrEnum):
    SOURCE_NO_LONGER_ELIGIBLE = "The source no longer has eligible exchanges."


def file_source(plan: FilingPlan, members: staff.StaffSnapshot, reload: ReloadExchanges
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
        exchanges = tuple(exchange for exchange in reload()
                          if exchange.source_key == key and (_eligible(exchange) or plan.previously_filed))
        if not exchanges:
            return NoExchangeFiling.SOURCE_NO_LONGER_ELIGIBLE
        first = next((exchange for exchange in exchanges if exchange.direction is Direction.QUESTION_TO_NATE),
                     exchanges[0])
        destinations = tuple(dict.fromkeys((*plan.destinations, *(link for exchange in exchanges
                                                                 for link in exchange.list_links))))
        if len(destinations) > 1 or any(isinstance(link, LegacyListLink) for link in destinations):
            raise the_list.ListError("The source has no unique List destination.")
        link = destinations[0] if destinations else the_list.NextNumber.AVAILABLE
        assert not isinstance(link, LegacyListLink)
        actor = first.asker if first.direction is Direction.QUESTION_TO_NATE else first.answerer
        if not isinstance(link, investigation.ListDestination) and isinstance(actor, (Nate, RecordedAnswerer)):
            raise the_list.ListError("The source has no staff ID for the List owner.")
        owner = staff.StaffId(actor) if not isinstance(actor, RecordedAnswerer) else staff.StaffId("")
        if not first.question.strip():
            raise the_list.ListError("The source question has no text.")
        return the_list.file_exchanges(source, exchanges, review_submissions.submission_title(first.question),
                                       linked=link, owner=owner, members=members)


def _file_live(reload: ReloadExchanges, entries: Iterable[list_page.ListRecord],
               members: staff.StaffSnapshot) -> None:
    filed = ((investigation.ListDestination(entry.keeper, entry.item.number), exchange)
             for entry in entries for exchange in entry.item.filed_exchanges)
    for plan in _plans(reload(), filed):
        try:
            _ = file_source(plan, members, reload)
        except (OSError, ValueError) as error:
            key = plan.exchanges[0].source_key
            print(f"List exchange {key}: {error}", file=sys.stderr)


def _linked_items(key: str, entries: Iterable[list_page.ListRecord]) -> tuple[ListLink, ...]:
    return tuple(investigation.ListDestination(entry.keeper, entry.item.number) for entry in entries
                 if key in entry.item.reviews or (isinstance(entry.item.report, the_list.ListReport)
                                                  and entry.item.report.evidence_key == key))


def file_saved_question(record: update_registration.QuestionRecord, members: staff.StaffSnapshot) -> None:
    if "review_submission" in record.item and not record.item.get("followups"):
        return
    physical = review_items.question_record_key(record)
    with review_items.after_store_write(physical):
        entries = list_page.records(include_closed=True)
        links = _linked_items(physical, entries)
        zone = user_zone()

        def reload() -> Iterator[Exchange]:
            for current in update_registration.selected_question_records(
                    [record.path], ("open", "closed"), None, owner=record.owner):
                origin = current.item.get("review_submission")
                key = (f"submission/question/{new_item_dialog.read_submission(origin).request_id}"
                       if origin is not None else physical)
                yield from _native_question_exchanges(current, key, links, zone, members, the_list.exchange_history(key))

        _file_live(reload, entries, members)


def file_saved_submission(scoped: new_item_dialog.ScopedSubmissionKey, members: staff.StaffSnapshot) -> None:
    with review_items.after_store_write(scoped.item_id):
        with review_submissions.scoped_submission(scoped, members=members) as current:
            saved = current.saved()
        file_saved_review(review_items.submission_item(saved, members), members)


def file_saved_investigation(record: investigation.Investigation) -> None:
    with review_items.after_store_write(review_items.investigation_record_key(record)):
        members = staff.snapshot()
        path = investigation.review_record_path(record.owner, record.id)
        file_saved_review(review_items.investigation_item(record, members, path), members)


def file_saved_review(item: review_items.ReviewItem, members: staff.StaffSnapshot,
                      root: Path | None = None) -> None:
    zone = user_zone()
    history = the_list.exchange_history(_review_source_key(item))
    if not tuple(_review_item_exchanges(item, (), zone, members, history)):
        return
    entries = list_page.records(include_closed=True)
    linked = _linked_items(item.key, entries)
    _file_live(partial(_reload_review, root or change_routing.review_root(), item, members, linked, zone),
               entries, members)


def file_saved_list(keeper: staff.StaffId, item: the_list.ListItem) -> None:
    with review_items.after_store_write(f"{keeper}/{item.id}"):
        members = staff.snapshot()
        entry = list_page.ListRecord(keeper, item)
        _file_live(partial(_reload_list, keeper, item.id, members, user_zone()), (entry,), members)


def file_saved_picture(root: Path, identity: str) -> None:
    with review_items.after_store_write(f"image/{identity}"):
        entries = list_page.records(include_closed=True)
        keepers: dict[int, list[staff.StaffId]] = defaultdict(list)
        for entry in entries:
            keepers[entry.item.number].append(entry.keeper)
        _file_live(partial(_reload_picture, root, identity, keepers, user_zone()), entries, staff.snapshot())


def apply_review_handoffs(items: list[review_items.ReviewItem], zone: ZoneInfo, *,
                          sources: list[Path] | None = None,
                          members: staff.StaffSnapshot | None = None) -> list[review_items.ReviewItem]:
    """Use the bindings on List exchanges with a matching save receipt."""
    keys = {item.key: _review_source_key(item) for item in items if item.type in _REVIEW_SOURCE_KINDS}
    if not keys:
        return items
    members = staff.snapshot() if members is None else members
    by_key = set(keys.values())
    index: dict[str, list[tuple[investigation.ListDestination, frozenset[list_exchange.FiledExchange]]]] = defaultdict(list)
    history: dict[str, list[list_exchange.FiledExchange]] = defaultdict(list)
    for entry in list_page.records(include_closed=True, sources=sources):
        by_source: dict[str, list[list_exchange.FiledExchange]] = defaultdict(list)
        for exchange in entry.item.filed_exchanges:
            if exchange.source_key in by_key:
                by_source[exchange.source_key].append(exchange)
        destination = investigation.ListDestination(entry.keeper, entry.item.number)
        for key, saved in by_source.items():
            history[key].extend(saved)
            confirmed = the_list.confirmed_exchanges(key, destination, saved, sources=sources)
            if confirmed:
                index[key].append((destination, confirmed))
    projected: list[review_items.ReviewItem] = []
    for item in items:
        filed = index.get(keys.get(item.key, ""), ())
        if len(filed) != 1:
            projected.append(item)
            continue
        destination, saved = filed[0]
        with review_items.after_store_write(item.key):
            key = keys[item.key]
            previous = the_list.combine_exchange_history(tuple(history[key]), the_list.pending_exchanges(key))
            verifier = ExchangeVerifier(item, zone, members, previous)
            handoff = verifier.review_handoff(item, key, destination, saved)
        projected.append(replace(item, exchange_handoff=handoff))
    return projected


def _unresolved_lines(inventory: ExchangeInventory) -> Iterator[str]:
    yield f"Unresolved legacy replies: {sum(count for _, count in inventory.unresolved)}"
    for key, count in inventory.unresolved:
        yield f"  {key}: {count}"


def migrate(inventory: ExchangeInventory, members: staff.StaffSnapshot, *, apply: bool) -> int:
    groups: dict[tuple[str, investigation.ListDestination], list[list_exchange.FiledExchange]] = defaultdict(list)
    for destination, exchange in inventory.filed:
        groups[exchange.source_key, destination].append(exchange)
    saved = {exchange for (key, destination), group in groups.items()
             for exchange in the_list.confirmed_exchanges(key, destination, group)}
    failures = 0
    for source in Source:
        plans = tuple(_plans((exchange for exchange in inventory.exchanges if exchange.source is source), inventory.filed))
        pending = tuple(plan for plan in plans if not all(list_exchange.FiledExchange.from_exchange(exchange) in saved for exchange in plan.exchanges))
        print(f"{source}: sources={len(pending)} exchanges={sum(len(plan.exchanges) for plan in pending)} "
              + f"linked={sum(bool(plan.destinations) for plan in pending)} "
              + f"new-rows={sum(not plan.destinations for plan in pending)} unchanged={len(plans) - len(pending)}")
        if not apply:
            continue
        for plan in pending:
            key = plan.exchanges[0].source_key
            try:
                link = file_source(plan, members, inventory.reload[key])
            except (OSError, ValueError) as error:
                failures += 1
                print(f"  {key}: {error}", file=sys.stderr)
            else:
                if isinstance(link, investigation.ListDestination):
                    print(f"  {key} -> {link.keeper}/L{link.number}")
    for line in _unresolved_lines(inventory):
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
    inventory = read_exchanges(change_routing.review_root(), members=members)
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

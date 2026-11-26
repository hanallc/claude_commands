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


def _reply_positions(fields: update_registration.OutstandingItem, log: tuple[ReviewLog, ...],
                     zone: ZoneInfo, history: tuple[list_exchange.FiledExchange, ...]) -> dict[int, int]:
    """Claim each answer event once, in order of evidence."""
    replies = fields.get("followups", [])
    positions: dict[int, int] = {}
    claimed: set[int] = set()
    disputed: set[int] = set()

    def assign(candidates: dict[int, list[int]]) -> None:
        owners: dict[int, list[int]] = defaultdict(list)
        for index, matches in candidates.items():
            for position in matches:
                owners[position].append(index)
        disputed.update(position for position, indexes in owners.items() if len(indexes) > 1)
        for index, matches in candidates.items():
            if len(matches) == 1 and matches[0] not in disputed:
                positions[index] = matches[0]
                claimed.add(matches[0])

    def eligible(index: int, position: int) -> bool:
        return (index not in positions and position not in claimed and position not in disputed and position in range(len(log))
                and _answer_event(log[position]) and log[position].words.rstrip() == "Answer: " + replies[index]["reply"])

    saved = {exchange.exchange_id: exchange.binding for exchange in history}
    stored: dict[int, list[int]] = defaultdict(list)
    for index in range(len(replies)):
        binding = saved.get(f"followup:{index + 1}")
        if (isinstance(binding, ExchangeBinding) and isinstance(binding.question, QuestionRecord)
                and (position := _event_position(binding.reply, log)) is not None and eligible(index, position)):
            stored[index].append(position)
    assign(stored)
    assign({index: [position] for index in range(len(replies))
            if eligible(index, position := fields.get("reply_log_positions", {}).get(str(index), -1))})
    assign({index: [position for position, event in enumerate(log)
                    if event.feedback_request_id == question_path.answer_log_id(identity) and eligible(index, position)]
            for index, reply in enumerate(replies) if (identity := reply.get("question_id"))})
    timed: dict[int, list[int]] = {}
    for index, reply in enumerate(replies):
        if index in positions:
            continue
        stamp = reply.get("replied_at") or (fields.get("answered_at") if index == len(replies) - 1 else None)
        if stamp is not None:
            time = review_items.moment(stamp, zone)
            timed[index] = [position for position, event in enumerate(log)
                            if not event.feedback_request_id and event.time == time and eligible(index, position)]
    assign(timed)
    return positions


def _answerer(reference: str, members: staff.StaffSnapshot) -> staff.StaffId | RecordedAnswerer:
    member = members.by_member_reference(reference)
    if member is None:
        matches = [row for row in (*members.rows, *members.retired_producers)
                   if reference in (row.id, row.name)]
        member = matches[0] if len(matches) == 1 else None
    return member.id if member is not None else RecordedAnswerer(reference)


def _question_exchanges(record: update_registration.QuestionRecord, source: Source, key: str,
                        links: tuple[ListLink, ...], zone: ZoneInfo, members: staff.StaffSnapshot,
                        history: tuple[list_exchange.FiledExchange, ...],
                        unresolved: list[tuple[str, int]] | None) -> Iterator[Exchange]:
    fields = record.item
    initial = fields.get("question", fields["text"])
    filed = review_items.moment(fields["since"], zone)
    log = review_items.dated_review_log(fields.get("review_log", ""), zone)
    closed = record.status == "closed"
    if source is Source.STAFF_QUESTION:
        answered = next((event for event in reversed(log)
                         if event.who == Nate.USER and event.words.startswith("answer: ")), None)
        answer = (ExchangeAnswer(answered.words.removeprefix("answer: "), answered.time, AnswerRead.NOT_TRACKED)
                  if answered is not None else QuestionWithoutAnswer.CLOSED if closed else QuestionWithoutAnswer.OPEN)
        yield Exchange(source, key, "initial", Direction.QUESTION_TO_NATE, initial, filed, record.asker,
                       Nate.USER, answer, links, binding=ExchangeBinding(QuestionRecord("initial"),
                           ReplyWithoutEvent.NATIVE_RECORD if isinstance(answer, ExchangeAnswer) else ReplyWithoutEvent.NO_ANSWER))
    replies = fields.get("followups", [])
    positions = _reply_positions(fields, log, zone, history)
    if unresolved is not None:
        claimed = set(positions.values())
        if count := sum(index not in claimed and _answer_event(event) for index, event in enumerate(log)):
            unresolved.append((key, count))
    read_times = tuple(_read_times(log))
    previous = 0
    for index, reply in enumerate(replies):
        position = positions.get(index)
        stamp = reply.get("replied_at")
        if stamp is None and position is None and index == len(replies) - 1:
            stamp = fields.get("answered_at")
        answered_at = (log[position].time if position is not None else review_items.moment(stamp, zone)
                       if stamp is not None else TimeNotRecorded.ABSENT)
        end = position if position is not None else min(
            (pos for later, pos in positions.items() if later > index), default=len(log))
        prompt = next((event for event in reversed(log[previous:end])
                       if event.who == Nate.USER and event.words.partition(": ")[0] in ("question", "followup")
                       and " ".join(event.words.partition(": ")[2].split()) == " ".join(reply["followup"].split())),
                      None)
        if position is not None:
            previous = position + 1
        asked_at = (prompt.time if prompt is not None else filed
                    if source is Source.SCOPED_REPLY and index == 0 and reply["followup"] == initial
                    else TimeNotRecorded.ABSENT)
        answer = ExchangeAnswer(reply["reply"], answered_at, _read_state(answered_at, read_times, acknowledged=closed))
        event = log[position] if position is not None else None
        author = _answerer(event.who, members) if event is not None else record.owner
        identity = f"followup:{index + 1}"
        binding = ExchangeBinding(QuestionRecord(identity),
                                  _reply_origin(position, log[position]) if position is not None else ReplyWithoutEvent.NATIVE_RECORD)
        yield Exchange(source, key, identity, Direction.ANSWER_TO_NATE, reply["followup"],
                       asked_at, Nate.USER, author, answer, links, binding=binding)


@dataclass(frozen=True)
class _Prompt:
    origin: QuestionOrigin
    identity: str
    text: str
    time: datetime


def _event_digest(event: ReviewLog) -> str:
    parts = (event.time.astimezone(UTC).isoformat(), event.who, event.feedback_request_id, event.words)
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


def _reply_origin(position: int, event: ReviewLog) -> TaggedReply | LoggedReply:
    return TaggedReply(event.feedback_request_id) if event.feedback_request_id else LoggedReply(position, _event_digest(event))


def _answer_event(event: ReviewLog) -> bool:
    return event.who != Nate.USER and event.words.startswith("Answer: ") and bool(event.words[8:].strip())


def _event_position(reply: ReplyOrigin, log: tuple[ReviewLog, ...]) -> int | None:
    if isinstance(reply, LoggedReply):
        if reply.position < len(log) and _event_digest(log[reply.position]) == reply.digest:
            return reply.position
        matches = [index for index, event in enumerate(log) if _answer_event(event) and _event_digest(event) == reply.digest]
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


def _untagged_windows(log: tuple[ReviewLog, ...], prompts: dict[int, _Prompt]) -> dict[int, tuple[int, ...]]:
    """Find possible replies within each outgoing request's log interval."""
    windows: dict[int, list[int]] = {index: [] for index in prompts}
    question = -1
    for index, event in enumerate(log):
        if index in prompts:
            question = index
        elif event.who == Nate.USER and event.words.partition(": ")[0].lower() in (
                "feedback", "feedback sent", "approve", "decline", "answer"):
            question = -1
        elif question >= 0 and not event.feedback_request_id and _answer_event(event):
            windows[question].append(index)
    return {index: tuple(positions) for index, positions in windows.items()}


def _competing_request(log: tuple[ReviewLog, ...], before: int, *,
                       bound: Iterable[str] = ()) -> bool:
    """Check for an earlier request that could also own an untagged reply."""
    earlier = log[:before]
    answered = {event.feedback_request_id.removeprefix("answer:") for event in earlier
                if event.feedback_request_id and _answer_event(event)}
    for index, event in enumerate(earlier):
        label = event.words.partition(": ")[0].lower()
        if event.who != Nate.USER or label not in ("question", "followup", "feedback"):
            continue
        tag = event.feedback_request_id
        identity = "question:" + tag if tag else f"log:{index}"
        if tag not in answered and identity not in bound:
            return True
    return False


def _slot_prompt(slot: delivery_state.AnsweredQuestion, prompts: dict[int, _Prompt], filed: datetime) -> _Prompt:
    identity = "question:" + slot.question_id
    tagged = [prompt for prompt in prompts.values() if prompt.identity == identity]
    if len(tagged) == 1:
        return tagged[0]
    return _Prompt(QuestionSlot(slot.question_id), identity, slot.text, filed)


def _legacy_reply(question: int, answer: ExchangeAnswer, author: staff.StaffId | RecordedAnswerer | Nate,
                  log: tuple[ReviewLog, ...], windows: dict[int, tuple[int, ...]],
                  members: staff.StaffSnapshot, *, bound: Iterable[str]) -> int | None:
    """Use independent slot evidence before checking the reply text."""
    positions = windows[question]
    if isinstance(answer.time, datetime):
        positions = tuple(position for position in positions if log[position].time == answer.time)
    elif _competing_request(log, question, bound=bound):
        return None
    if not isinstance(author, (RecordedAnswerer, Nate)):
        positions = tuple(position for position in positions if _answerer(log[position].who, members) == author)
    if len(positions) == 1 and log[positions[0]].words == "Answer: " + answer.text:
        return positions[0]
    return None


def _saved_scoped_exchange(saved: list_exchange.FiledExchange, log: tuple[ReviewLog, ...],
                           prompts: dict[int, _Prompt], links: tuple[ListLink, ...],
                           read_times: tuple[datetime, ...], acknowledged: bool,
                           members: staff.StaffSnapshot) -> Exchange:
    answer = saved.answer
    binding = saved.binding
    asked_at, answerer = saved.asked_at, saved.answerer
    if isinstance(binding, ExchangeBinding) and isinstance(answer, ExchangeAnswer):
        if isinstance(binding.question, (QuestionEvent, QuestionSlot)):
            prompt = _bound_prompt(binding.question, saved.question, prompts)
            if prompt is not None:
                asked_at = prompt.time
        position = _event_position(binding.reply, log)
        if position is not None and log[position].words == "Answer: " + answer.text:
            answerer = _answerer(log[position].who, members)
        state = _read_state(answer.time, read_times, acknowledged=acknowledged)
        if state is AnswerRead.READ:
            answer = replace(answer, read=state)
    return Exchange(saved.source, saved.source_key, saved.exchange_id, saved.direction, saved.question,
                    asked_at, saved.asker, answerer, answer, links, binding=binding)


def _bound_prompt(origin: QuestionEvent | QuestionSlot, text: str, prompts: dict[int, _Prompt]) -> _Prompt | None:
    if isinstance(origin, QuestionSlot):
        matches = [prompt for prompt in prompts.values() if prompt.origin == origin]
        prompt = matches[0] if len(matches) == 1 else None
    else:
        prompt = prompts.get(origin.position)
        if prompt is not None and not isinstance(prompt.origin, QuestionEvent):
            return None
    return prompt if prompt is not None and prompt.text == text else None


def _legacy_match(identity: str, text: str, answer: ExchangeAnswer,
                  author: staff.StaffId | RecordedAnswerer | Nate, log: tuple[ReviewLog, ...],
                  prompts: dict[int, _Prompt], windows: dict[int, tuple[int, ...]], members: staff.StaffSnapshot,
                  bound: set[str]) -> tuple[_Prompt, int] | None:
    matching = [(index, prompt) for index, prompt in prompts.items()
                if prompt.identity not in bound and prompt.text == text]
    exact = [(index, prompt) for index, prompt in matching if prompt.identity == identity]
    if exact:
        matching = exact
    elif identity.startswith("question:") and isinstance(answer.time, datetime):
        matching = [(index, prompt) for index, prompt in matching if isinstance(prompt.origin, QuestionEvent)]
    else:
        return None
    if len(matching) != 1:
        return None
    index, prompt = matching[0]
    if not exact and _competing_request(log, index, bound=bound):
        return None
    position = _legacy_reply(index, answer, author, log, windows, members, bound=bound)
    if position is None:
        return None
    if not exact:
        prompt = replace(prompt, origin=QuestionEvent(index, identity.removeprefix("question:")))
    return prompt, position


def _scoped_log_exchanges(item: review_items.ReviewItem, key: str, links: tuple[ListLink, ...],
                          zone: ZoneInfo, members: staff.StaffSnapshot, history: tuple[list_exchange.FiledExchange, ...],
                          unresolved: list[tuple[str, int]] | None) -> Iterator[Exchange]:
    """File verified reply origins and keep the saved pairing across later requests."""
    source = item.source
    log = _native_log(item, zone)
    prompts = _question_prompts(log)
    windows = _untagged_windows(log, prompts)
    possible = {position for positions in windows.values() for position in positions}
    read_times = tuple(_read_times(log))
    acknowledged = item.state is review_items.ReviewState.DONE
    if isinstance(source, review_items.InvestigationSource) and source.record.read:
        read_times += (review_items.moment(source.record.read, zone),)

    def exchange(prompt: _Prompt, author: staff.StaffId | RecordedAnswerer, answer: ExchangeAnswer,
                 reply: ReplyOrigin) -> Exchange:
        return Exchange(Source.SCOPED_REPLY, key, prompt.identity, Direction.ANSWER_TO_NATE,
                        prompt.text, prompt.time, Nate.USER, author, answer, links,
                        binding=ExchangeBinding(prompt.origin, reply))

    def from_event(prompt: _Prompt, position: int) -> Exchange:
        event = log[position]
        state = _read_state(event.time, read_times, acknowledged=acknowledged)
        return exchange(prompt, _answerer(event.who, members), ExchangeAnswer(event.words[8:], event.time, state),
                        _reply_origin(position, event))

    accepted: dict[str, Exchange] = {}
    claimed: set[int] = set()
    bound_prompts: set[str] = set()
    bound_slots: set[str] = set()

    def accept(value: Exchange) -> None:
        accepted[value.exchange_id] = value
        binding = value.binding
        if isinstance(binding, ExchangeBinding):
            if isinstance(binding.question, (QuestionEvent, QuestionSlot)):
                prompt = _bound_prompt(binding.question, value.question, prompts)
                if prompt is not None:
                    bound_prompts.add(prompt.identity)
            if binding.slot_id:
                bound_slots.add(binding.slot_id)
            position = _event_position(binding.reply, log)
            if position is not None:
                claimed.add(position)

    initial = None
    if isinstance(source, review_items.InvestigationSource):
        record = source.record
        if (record.purpose in (investigation.InvestigationPurpose.INVESTIGATE,
                              investigation.InvestigationPurpose.LIST_INVESTIGATION)
                and record.kind is investigation.InvestigationKind.INVESTIGATION):
            prompt = _Prompt(QuestionRecord("initial"), "initial", record.question, item.times.filed)
            boundary = next((index for index, event in enumerate(log) if event.who == Nate.USER and
                             event.words.partition(": ")[0].lower() in ("question", "followup", "feedback", "feedback sent")),
                            len(log))
            answers = [index for index, event in enumerate(log) if _answer_event(event)]
            earlier = [index for index in answers if index < boundary and not log[index].feedback_request_id]
            if len(earlier) == 1:
                initial = from_event(prompt, earlier[0])
            elif not answers and boundary == len(log) and record.answer and record.answered:
                time = review_items.moment(record.answered, zone)
                state = _read_state(time, read_times, acknowledged=acknowledged)
                initial = exchange(prompt, record.owner, ExchangeAnswer(record.answer, time, state),
                                   ReplyWithoutEvent.NATIVE_RECORD)

    saved_initial = next((value for value in history if value.exchange_id == "initial"), None)
    suspect = None
    if isinstance(source, review_items.InvestigationSource):
        if saved_initial is None:
            if initial is not None:
                accept(initial)
        elif (initial is not None and initial.question == saved_initial.question
              and isinstance(initial.answer, ExchangeAnswer) and isinstance(saved_initial.answer, ExchangeAnswer)
              and initial.answer.text == saved_initial.answer.text
              and (not isinstance(saved_initial.answer.time, datetime)
                   or isinstance(initial.answer.time, datetime) and saved_initial.answer.time <= initial.answer.time)):
            time = saved_initial.answer.time
            if isinstance(time, datetime):
                initial = replace(initial, answer=replace(initial.answer, time=time))
            accept(initial)
        elif isinstance(saved_initial.answer, ExchangeAnswer):
            # Preserve the saved row for repair without assigning its answer to another question.
            suspect = saved_initial.answer
        possible.update(index for index, event in enumerate(log) if _answer_event(event))

    legacy = {saved.exchange_id: saved for saved in history if saved.binding is UnboundExchange.LEGACY}
    if isinstance(source, review_items.InvestigationSource):
        _ = legacy.pop("initial", None)
    for saved in history:
        if isinstance(source, review_items.InvestigationSource) and saved.exchange_id == "initial":
            continue
        if isinstance(saved.binding, ExchangeBinding):
            accept(_saved_scoped_exchange(saved, log, prompts, links, read_times, acknowledged, members))

    slot = source.slots.question if isinstance(source, review_items.ChangeSource) else delivery_state.NoQuestion()
    current_prompt = None
    if isinstance(slot, delivery_state.AnsweredQuestion) and not isinstance(slot, review_items.CompletedFeedback):
        current_prompt = _slot_prompt(slot, prompts, item.times.filed)
    tagged: dict[str, list[int]] = defaultdict(list)
    for position, event in enumerate(log):
        if event.feedback_request_id and _answer_event(event):
            tagged[event.feedback_request_id.removeprefix("answer:")].append(position)

    def first_filing(value: Exchange) -> None:
        binding = value.binding
        assert isinstance(binding, ExchangeBinding)
        aliases = (value.exchange_id, "question:" + binding.slot_id) if binding.slot_id else (value.exchange_id,)
        old = [legacy[identity] for identity in dict.fromkeys(aliases) if identity in legacy]
        if not old:
            accept(value)
        elif len(old) == 1:
            saved = old[0]
            answer = saved.answer
            if (saved.question == value.question and isinstance(answer, ExchangeAnswer)
                    and isinstance(value.answer, ExchangeAnswer) and answer.text == value.answer.text):
                del legacy[saved.exchange_id]
                accept(replace(value, exchange_id=saved.exchange_id))

    tagged_prompts: dict[str, list[_Prompt]] = defaultdict(list)
    for prompt in prompts.values():
        if prompt.identity.startswith("question:"):
            tagged_prompts[prompt.identity.removeprefix("question:")].append(prompt)
    if current_prompt is not None:
        assert isinstance(slot, delivery_state.AnsweredQuestion)
        if slot.question_id not in tagged_prompts:
            tagged_prompts[slot.question_id].append(current_prompt)
    for tag, matches in tagged_prompts.items():
        positions = tagged.get(tag, [])
        if len(matches) == len(positions) == 1 and tag not in bound_slots and matches[0].identity not in bound_prompts:
            first_filing(from_event(matches[0], positions[0]))

    if (isinstance(source, review_items.ChangeSource) and isinstance(slot, delivery_state.AnsweredQuestion)
            and current_prompt is not None and slot.question_id not in bound_slots
            and current_prompt.identity not in bound_prompts):
        time = source.answered_at if isinstance(source.answered_at, datetime) else TimeNotRecorded.ABSENT
        reference = source.answered_by or (source.validators[0] if source.validators else "")
        author = _answerer(reference, members) if reference else RecordedAnswerer("Answerer not recorded")
        answer = ExchangeAnswer(slot.answer, time, _read_state(time, read_times, acknowledged=acknowledged))
        verified_author = author if source.answered_by else RecordedAnswerer("Answerer not recorded")
        previous = legacy.get(current_prompt.identity)
        evidence = answer
        if previous is not None and isinstance(previous.answer, ExchangeAnswer) and previous.answer.text == answer.text:
            evidence = replace(answer, time=previous.answer.time) if not isinstance(answer.time, datetime) else answer
        match = _legacy_match(current_prompt.identity, current_prompt.text, evidence, verified_author,
                              log, prompts, windows, members, bound_prompts)
        if match is not None and match[1] not in claimed:
            first_filing(from_event(*match))
        else:
            first_filing(exchange(current_prompt, author, answer, ReplyWithoutEvent.SLOT))

    # Upgrade old IDs only when one filed ID matches one verified event.
    upgrades: dict[int, list[Exchange]] = defaultdict(list)
    for saved in legacy.values():
        if isinstance(saved.answer, ExchangeAnswer):
            match = _legacy_match(saved.exchange_id, saved.question, saved.answer, saved.answerer,
                                  log, prompts, windows, members, bound_prompts)
            if match is not None and match[1] not in claimed:
                prompt, position = match
                upgrades[position].append(replace(from_event(prompt, position), exchange_id=saved.exchange_id))
    for matches in upgrades.values():
        if len(matches) == 1:
            del legacy[matches[0].exchange_id]
            accept(matches[0])
    for saved in legacy.values():
        accept(_saved_scoped_exchange(saved, log, prompts, links, read_times, acknowledged, members))

    if not isinstance(source, review_items.ChangeSource):
        for index, prompt in prompts.items():
            positions = windows[index]
            if (len(positions) == 1 and positions[0] not in claimed and prompt.identity not in bound_prompts
                    and prompt.identity not in accepted and not _competing_request(log, index, bound=bound_prompts)):
                position = positions[0]
                if suspect is None or log[position].words != "Answer: " + suspect.text:
                    accept(from_event(prompt, position))

    if unresolved is not None:
        first_unbound = min((index for index, prompt in prompts.items() if prompt.identity not in bound_prompts),
                            default=len(log))
        possible.update(index for index in range(first_unbound + 1, len(log))
                        if not log[index].feedback_request_id and _answer_event(log[index]))
        count = len(possible - claimed)
        if count or suspect is not None:
            unresolved.append((key, count or 1))
    yield from accepted.values()


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
        yield from _scoped_log_exchanges(item, key, tuple(destinations), zone, members, history, unresolved)


def _native_question_exchanges(record: update_registration.QuestionRecord, key: str, links: tuple[ListLink, ...],
                               zone: ZoneInfo, members: staff.StaffSnapshot,
                               history: tuple[list_exchange.FiledExchange, ...] = (),
                               unresolved: list[tuple[str, int]] | None = None) -> Iterator[Exchange]:
    kind = Source.SCOPED_REPLY if "review_submission" in record.item else Source.STAFF_QUESTION
    yield from _question_exchanges(record, kind, key, links, zone, members, history, unresolved)


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


def _handoff_content(exchange: list_exchange.FiledExchange) -> list_exchange.FiledExchange:
    answer = exchange.answer
    if isinstance(answer, ExchangeAnswer):
        return replace(exchange, answer=replace(answer, read=AnswerRead.UNKNOWN), legacy_binding="")
    return replace(exchange, legacy_binding="")


def _handoff_reply_events(item: review_items.ReviewItem, saved: Iterable[list_exchange.FiledExchange],
                          zone: ZoneInfo) -> tuple[frozenset[int], str]:
    log = _native_log(item, zone)
    offset = len(item.log) - len(log)
    positions: set[int] = set()
    filed_slot_id = ""
    for exchange in saved:
        binding, answer = exchange.binding, exchange.answer
        if not isinstance(binding, ExchangeBinding) or not isinstance(answer, ExchangeAnswer):
            continue
        if binding.slot_id:
            slot = item.source.slots.question if isinstance(item.source, review_items.ChangeSource) else delivery_state.NoQuestion()
            if (isinstance(slot, delivery_state.AnsweredQuestion) and not isinstance(slot, review_items.CompletedFeedback)
                    and (slot.question_id, slot.text, slot.answer) == (binding.slot_id, exchange.question, answer.text)):
                filed_slot_id = binding.slot_id
                synthetic_tag = question_path.answer_log_id(binding.slot_id)
                positions.update(index for index, event in enumerate(item.log[:max(0, offset)])
                                 if event.feedback_request_id == synthetic_tag)
        position = _event_position(binding.reply, log)
        if position is not None and offset >= 0 and log[position].words == "Answer: " + answer.text:
            native, displayed = log[position], item.log[offset + position]
            if (native.time, native.who, native.feedback_request_id) == (
                    review_items.aware(displayed.time, zone), displayed.who, displayed.feedback_request_id):
                positions.add(offset + position)
    return frozenset(positions), filed_slot_id


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
    for entry in list_page.records(include_closed=True, sources=sources):
        by_source: dict[str, list[list_exchange.FiledExchange]] = defaultdict(list)
        for exchange in entry.item.filed_exchanges:
            if exchange.source_key in by_key:
                by_source[exchange.source_key].append(exchange)
        destination = investigation.ListDestination(entry.keeper, entry.item.number)
        for key, saved in by_source.items():
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
        source = item.source
        native_question = isinstance(source, (review_items.QuestionSource, review_items.UnownedQuestionSource))
        investigation_card = isinstance(source, review_items.InvestigationSource)
        with review_items.after_store_write(item.key):
            if native_question or investigation_card:
                unresolved: list[tuple[str, int]] = []
                exchanges = tuple(exchange for exchange in _review_item_exchanges(
                    item, (), zone, members, tuple(saved), unresolved) if _eligible(exchange))
                current = {_handoff_content(list_exchange.FiledExchange.from_exchange(exchange))
                           for exchange in exchanges}
                matched = tuple(exchange for exchange in saved if _handoff_content(exchange) in current)
                waits = item.state is review_items.ReviewState.WAITING
                hides_card = (bool(matched) and len(matched) == len(exchanges)
                              and not waits and not unresolved)
            else:
                matched = tuple(exchange for exchange in saved if isinstance(exchange.binding, ExchangeBinding))
                hides_card = False
            if not matched:
                projected.append(item)
                continue
            events, slot_id = _handoff_reply_events(item, matched, zone)
        ids = frozenset(exchange.exchange_id for exchange in matched if isinstance(exchange.answer, ExchangeAnswer))
        open_staff_question = any(exchange.direction is Direction.QUESTION_TO_NATE
                                  and exchange.answer is QuestionWithoutAnswer.OPEN for exchange in matched)
        filed_investigation_answer = False
        if isinstance(source, review_items.InvestigationSource) and source.record.answered:
            native = _native_log(item, zone)
            logged_answers = [index for index, event in enumerate(native) if _answer_event(event)]
            if logged_answers:
                last = logged_answers[-1]
                filed_investigation_answer = (native[last].words == "Answer: " + source.record.answer
                    and len(item.log) - len(native) + last in events)
            else:
                filed_investigation_answer = "initial" in ids
        handoff = review_items.FiledReviewExchange(destination, hides_card, events, ids, open_staff_question,
                                                   slot_id, filed_investigation_answer)
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

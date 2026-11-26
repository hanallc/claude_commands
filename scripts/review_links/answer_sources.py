"""Read the saved questions and answers that belong on the Studio List."""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from functools import partial
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.lib.user_zone import user_zone
from scripts.production import change_routing, delivery_state, investigation, list_exchange, proposal_issue, staff, the_list, update_registration
from scripts.production.list_exchange import (
    AnswerRead as AnswerRead, Direction as Direction, ExchangeAnswer as ExchangeAnswer, Nate as Nate,
    QuestionWithoutAnswer as QuestionWithoutAnswer, RecordedAnswerer as RecordedAnswerer, Source as Source,
    TimeNotRecorded as TimeNotRecorded,
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
    reply_event: ReviewLog | None = field(default=None, compare=False)


type ReloadExchanges = Callable[[], Iterable[Exchange]]


@dataclass(frozen=True)
class ExchangeInventory:
    exchanges: tuple[Exchange, ...]
    list_items: frozenset[investigation.ListDestination]
    filed: tuple[tuple[investigation.ListDestination, list_exchange.FiledExchange], ...] = ()
    reload: dict[str, ReloadExchanges] = field(default_factory=dict, compare=False)


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


def _reply_positions(fields: update_registration.OutstandingItem, log: tuple[ReviewLog, ...]) -> dict[int, int]:
    """Match saved answer events before searching the older unlinked log entries."""
    replies = fields.get("followups", [])
    positions = {index: position for index, exchange in enumerate(replies)
                 if (position := fields.get("reply_log_positions", {}).get(str(index), -1)) in range(len(log))
                 and log[position].words.rstrip() == "Answer: " + exchange["reply"]}
    named = {event.feedback_request_id: position for position, event in enumerate(log)
             if event.feedback_request_id and event.words.startswith("Answer: ")}
    for index, exchange in enumerate(replies):
        identity = exchange.get("question_id")
        if index not in positions and identity is not None:
            position = named.get(question_path.answer_log_id(identity))
            if position is not None and log[position].words.rstrip() == "Answer: " + exchange["reply"]:
                positions[index] = position
    reserved = set(positions.values())
    cursor = 0
    for index, exchange in enumerate(replies):
        if index in positions:
            cursor = max(cursor, positions[index] + 1)
            continue
        word = "Answer: " + exchange["reply"]
        for position in range(cursor, len(log)):
            event = log[position]
            if position not in reserved and not event.feedback_request_id and event.words.rstrip() == word:
                positions[index] = position
                cursor = position + 1
                break
    return positions


def _answerer(reference: str, members: staff.StaffSnapshot) -> staff.StaffId | RecordedAnswerer:
    member = members.by_member_reference(reference)
    if member is None:
        matches = [row for row in (*members.rows, *members.retired_producers)
                   if reference in (row.id, row.name)]
        member = matches[0] if len(matches) == 1 else None
    return member.id if member is not None else RecordedAnswerer(reference)


def _question_exchanges(record: update_registration.QuestionRecord, source: Source, key: str,
                        links: tuple[ListLink, ...], zone: ZoneInfo, members: staff.StaffSnapshot) -> Iterator[Exchange]:
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
                       Nate.USER, answer, links)
    replies = fields.get("followups", [])
    positions = _reply_positions(fields, log)
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
        yield Exchange(source, key, f"followup:{index + 1}", Direction.ANSWER_TO_NATE, reply["followup"],
                       asked_at, Nate.USER, author, answer, links, event)


@dataclass(frozen=True)
class _LoggedQuestion:
    identity: str
    text: str
    time: datetime


def _scoped_log_exchanges(item: review_items.ReviewItem, key: str, links: tuple[ListLink, ...],
                          zone: ZoneInfo, members: staff.StaffSnapshot) -> Iterator[Exchange]:
    """Match each saved reply to its question."""
    source = item.source
    pending: _LoggedQuestion | None = None
    if isinstance(source, review_items.InvestigationSource):
        record = source.record
        events = record.log
        if (record.purpose in (investigation.InvestigationPurpose.INVESTIGATE,
                               investigation.InvestigationPurpose.LIST_INVESTIGATION)
                and record.kind is investigation.InvestigationKind.INVESTIGATION):
            pending = _LoggedQuestion("initial", record.question, item.times.filed)
        if record.answer and record.answered and not any(event.words == "Answer: " + record.answer for event in events):
            events += (ReviewLog(review_items.moment(record.answered, zone), record.owner, "Answer: " + record.answer),)
        extra_reads = (review_items.moment(record.read, zone),) if record.read else ()
    elif isinstance(source, review_items.ChangeSource):
        events = review_items.dated_review_log(source.row.get("Review log", ""), zone)
        extra_reads = ()
    elif isinstance(source, review_items.ProposalSource):
        events = proposal_issue.review_log(source.record)
        extra_reads = ()
    else:
        events = item.log
        extra_reads = ()
    log = tuple(ReviewLog(review_items.aware(event.time, zone), event.who, event.words, event.feedback_request_id)
                for event in events)
    read_times = (*_read_times(log), *extra_reads)
    prompts = {index: _LoggedQuestion("question:" + event.feedback_request_id if event.feedback_request_id
                                     else f"log:{index}", event.words.partition(": ")[2], event.time)
               for index, event in enumerate(log) if event.who == Nate.USER and
               event.words.partition(": ")[0].lower() in ("question", "followup")
               and event.words.partition(": ")[2].strip()}
    named = {prompt.identity.removeprefix("question:"): prompt for prompt in prompts.values()
             if prompt.identity.startswith("question:")}
    slot = source.slots.question if isinstance(source, review_items.ChangeSource) else delivery_state.NoQuestion()
    slot_prompt: _LoggedQuestion | None = None
    slot_question_id = ""
    slot_position = -1
    if (isinstance(slot, (delivery_state.OpenQuestion, delivery_state.AnsweredQuestion))
            and not isinstance(slot, (review_items.OpenFeedback, review_items.CompletedFeedback))):
        slot_question_id = slot.question_id
        slot_position = next((index for index in prompts if log[index].feedback_request_id == slot_question_id),
                             len(log))
    legacy_slot = False
    slot_answered_at: question_path.AnswerTime = question_path.NO_ANSWER_TIME
    slot_author: staff.StaffId | RecordedAnswerer = RecordedAnswerer("Answerer not recorded")
    last_answer_position = -1
    if (isinstance(source, review_items.ChangeSource) and isinstance(slot, delivery_state.AnsweredQuestion)
            and not isinstance(slot, review_items.CompletedFeedback)):
        original = named.get(slot.question_id)
        slot_prompt = original or _LoggedQuestion("question:" + slot.question_id, slot.text, item.times.filed)
        named[slot.question_id] = slot_prompt
        slot_answered_at = source.answered_at
        reference = source.answered_by or (source.validators[0] if source.validators else "")
        if reference:
            slot_author = _answerer(reference, members)
        legacy_slot = original is None
        if legacy_slot and not isinstance(slot_answered_at, datetime):
            last_answer_position = max((index for index, event in enumerate(log)
                                        if event.who != Nate.USER and event.words.startswith("Answer: ")), default=-1)
    tagged_answers = {event.feedback_request_id.removeprefix("answer:") for event in log
                      if event.feedback_request_id and event.who != Nate.USER
                      and event.words.startswith("Answer: ") and event.words.removeprefix("Answer: ").strip()}
    seen: set[str] = set()
    logged_legacy_slot = False
    for index, event in enumerate(log):
        label, _, words = event.words.partition(": ")
        if index in prompts:
            tag = event.feedback_request_id
            # Only an older question can use an untagged reply after the slot changes.
            historical = index < slot_position and tag != slot_question_id and tag not in tagged_answers
            pending = prompts[index] if not tag or historical else None
            continue
        if (pending is not None and not pending.identity.startswith("question:") and event.who == Nate.USER
                and label.lower() in ("answer", "approve", "decline", "feedback", "feedback sent", "marked read", "read")):
            pending = None
        if label != "Answer" or event.who == Nate.USER or not words.strip():
            continue
        if event.feedback_request_id:
            prompt = named.get(event.feedback_request_id.removeprefix("answer:"))
        else:
            prompt, pending = pending, None
        if prompt is None or prompt.identity in seen:
            continue
        seen.add(prompt.identity)
        author = _answerer(event.who, members)
        mirrored_slot_exchange_id = ""
        if (legacy_slot and not logged_legacy_slot and isinstance(source, review_items.ChangeSource)
                and isinstance(slot, delivery_state.AnsweredQuestion) and prompt.identity.startswith("log:")
                and prompt.text == slot.text and words == slot.answer
                and (not source.answered_by or author == slot_author)
                and (event.time == slot_answered_at if isinstance(slot_answered_at, datetime)
                     else index == last_answer_position)):
            logged_legacy_slot = True
            mirrored_slot_exchange_id = "question:" + slot.question_id
        state = _read_state(event.time, read_times, acknowledged=item.state is review_items.ReviewState.DONE)
        yield Exchange(Source.SCOPED_REPLY, key, prompt.identity, Direction.ANSWER_TO_NATE, prompt.text,
                       prompt.time, Nate.USER, author, ExchangeAnswer(words, event.time, state), links, event,
                       mirrored_slot_exchange_id=mirrored_slot_exchange_id)
    if (slot_prompt is not None and isinstance(slot, delivery_state.AnsweredQuestion)
            and slot_prompt.identity not in seen and not logged_legacy_slot):
        answered_at = slot_answered_at if isinstance(slot_answered_at, datetime) else TimeNotRecorded.ABSENT
        answer = ExchangeAnswer(slot.answer, answered_at, _read_state(answered_at, read_times,
                                acknowledged=item.state is review_items.ReviewState.DONE))
        yield Exchange(Source.SCOPED_REPLY, key, slot_prompt.identity, Direction.ANSWER_TO_NATE, slot_prompt.text,
                       slot_prompt.time, Nate.USER, slot_author, answer, links)


_REVIEW_SOURCE_KINDS = (*review_items.QUESTION_TYPES, *review_items.INVESTIGATION_TYPES,
                       review_items.ReviewType.PROPOSAL, review_items.ReviewType.CHANGE, review_items.ReviewType.EFFICIENCY)


def _reload_review(root: Path, item: review_items.ReviewItem, members: staff.StaffSnapshot,
                   links: Iterable[ListLink], zone: ZoneInfo) -> Iterator[Exchange]:
    try:
        current = review_items.read_current(root, item, members, include_filed=True)
    except review_items.ReviewRecordMissing:
        return
    yield from _review_item_exchanges(current, links, zone, members)


def _review_source_key(item: review_items.ReviewItem) -> str:
    source = item.source
    origin = review_items.submission_origin(item)
    if isinstance(origin, new_item_dialog.ReviewSubmission):
        kind = ("question" if isinstance(source, (review_items.QuestionSource, review_items.UnownedQuestionSource))
                else "proposal" if isinstance(source, review_items.ProposalSource) else item.type.value.lower())
        return f"submission/{kind}/{origin.request_id}"
    return item.key


def _review_item_exchanges(item: review_items.ReviewItem, linked: Iterable[ListLink],
                           zone: ZoneInfo, members: staff.StaffSnapshot) -> Iterator[Exchange]:
    source = item.source
    destinations = list(linked)
    if (isinstance(source, review_items.InvestigationSource)
            and isinstance(destination := source.record.list_destination, investigation.ListDestination)
            and destination not in destinations):
        destinations.append(destination)
    key = _review_source_key(item)
    if isinstance(source, (review_items.QuestionSource, review_items.UnownedQuestionSource)):
        yield from _native_question_exchanges(source.record, key, tuple(destinations), zone, members)
    else:
        yield from _scoped_log_exchanges(item, key, tuple(destinations), zone, members)


def _native_question_exchanges(record: update_registration.QuestionRecord, key: str, links: tuple[ListLink, ...],
                               zone: ZoneInfo, members: staff.StaffSnapshot) -> Iterator[Exchange]:
    kind = Source.SCOPED_REPLY if "review_submission" in record.item else Source.STAFF_QUESTION
    yield from _question_exchanges(record, kind, key, links, zone, members)


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
                       ExchangeAnswer(answer.text, review_items.moment(answer.answered_at, zone), state), (link,))


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
                           question.recipient_id(staff.StaffId(record["owner"])), ExchangeAnswer(answer.text, time, state), links)


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
    exchanges: list[Exchange] = []
    reload: dict[str, ReloadExchanges] = {}
    for item in review_items.read(root, members=members, kinds=_REVIEW_SOURCE_KINDS, zone=zone,
                                 sources=[], include_filed=True):
        linked = tuple(links.get(item.key, ()))
        candidates = tuple(_review_item_exchanges(item, linked, zone, members))
        if candidates:
            reload[candidates[0].source_key] = partial(_reload_review, root, item, members, linked, zone)
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
    filed = tuple((investigation.ListDestination(entry.keeper, entry.item.number), exchange)
                  for entry in entries for exchange in entry.item.filed_exchanges)
    identified = list_exchange.retain_legacy_ids(tuple(exchanges), (exchange for _, exchange in filed))
    ordered = sorted(identified, key=lambda exchange: (exchange.source, exchange.source_key, exchange.exchange_id))
    return ExchangeInventory(tuple(ordered), frozenset(list_items), filed, reload)


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
                yield from _native_question_exchanges(current, key, links, zone, members)

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
    if not tuple(_review_item_exchanges(item, (), zone, members)):
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
        return replace(exchange, answer=replace(answer, read=AnswerRead.UNKNOWN))
    return exchange


def apply_review_handoffs(items: list[review_items.ReviewItem], zone: ZoneInfo, *,
                          sources: list[Path] | None = None,
                          members: staff.StaffSnapshot | None = None) -> list[review_items.ReviewItem]:
    """Use List exchanges that match the current native record."""
    members = staff.snapshot() if members is None else members
    candidates: dict[str, tuple[Exchange, ...]] = {}
    for item in items:
        with review_items.after_store_write(item.key):
            candidates[item.key] = tuple(exchange for exchange in _review_item_exchanges(item, (), zone, members)
                                         if _eligible(exchange))
    if not any(candidates.values()):
        return items
    index: dict[str, list[tuple[investigation.ListDestination, set[list_exchange.FiledExchange]]]] = defaultdict(list)
    for entry in list_page.records(include_closed=True, sources=sources):
        by_source: dict[str, list[list_exchange.FiledExchange]] = defaultdict(list)
        for exchange in entry.item.filed_exchanges:
            by_source[exchange.source_key].append(exchange)
        destination = investigation.ListDestination(entry.keeper, entry.item.number)
        for key, saved in by_source.items():
            confirmed = the_list.confirmed_exchanges(key, destination, saved, sources=sources)
            if confirmed:
                index[key].append((destination, {_handoff_content(exchange) for exchange in confirmed}))
    projected: list[review_items.ReviewItem] = []
    for item in items:
        exchanges = candidates.get(item.key, ())
        filed = index.get(exchanges[0].source_key, ()) if exchanges else ()
        if len(filed) != 1:
            projected.append(item)
            continue
        destination, saved = filed[0]
        identified = list_exchange.retain_legacy_ids(exchanges, saved)
        matched = tuple(exchange for exchange in identified
                        if _handoff_content(list_exchange.FiledExchange.from_exchange(exchange)) in saved)
        if not matched:
            projected.append(item)
            continue
        standalone = (item.type in review_items.QUESTION_TYPES
                      or item.type is review_items.ReviewType.INVESTIGATION)
        ids = frozenset(identity for exchange in matched if isinstance(exchange.answer, ExchangeAnswer)
                        for identity in (exchange.exchange_id, exchange.mirrored_slot_exchange_id) if identity)
        answer_events: set[int] = set()
        for exchange in matched:
            reply = exchange.reply_event
            question_id = (exchange.exchange_id.removeprefix("question:")
                           if exchange.exchange_id.startswith("question:") else "")
            for position, event in enumerate(item.log):
                if position in answer_events:
                    continue
                if (event == reply or
                        (reply is not None and reply.feedback_request_id
                         and event.feedback_request_id == reply.feedback_request_id)
                        or (question_id and event.feedback_request_id == question_path.answer_log_id(question_id))):
                    answer_events.add(position)
                    break
        open_staff_question = any(exchange.direction is Direction.QUESTION_TO_NATE
                                  and exchange.answer is QuestionWithoutAnswer.OPEN for exchange in matched)
        handoff = review_items.FiledReviewExchange(destination, standalone and len(matched) == len(exchanges),
                                                  frozenset(answer_events), ids, open_staff_question)
        projected.append(replace(item, exchange_handoff=handoff))
    return projected


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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

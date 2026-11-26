"""Read the saved questions and answers that belong on the Studio List."""
from __future__ import annotations

import argparse
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.lib.user_zone import user_zone
from scripts.production import change_routing, investigation, proposal_issue, staff, the_list, update_registration
from scripts.review_links import (
    image_store, images, list_page, new_item_dialog, question_path, question_record, review_items,
)
from scripts.review_links.review_events import ReviewLog


class Source(StrEnum):
    STAFF_QUESTION = "staff-question"
    SCOPED_REPLY = "sent-question/scoped-reply"
    LIST = "list"
    PICTURE = "picture"


class Direction(StrEnum):
    QUESTION_TO_NATE = "question to nate"
    ANSWER_TO_NATE = "answer to nate"


class AnswerRead(StrEnum):
    UNREAD = "unread"
    READ = "read"
    UNKNOWN = "read state not recorded"
    NOT_TRACKED = "the answer went to staff"


class TimeNotRecorded(StrEnum):
    ABSENT = "time not recorded"


class QuestionWithoutAnswer(StrEnum):
    OPEN = "waiting for an answer"
    CLOSED = "closed without a saved answer"


class Nate(StrEnum):
    USER = "nate"


@dataclass(frozen=True)
class RecordedAnswerer:
    """The List log records this name without a fixed staff ID."""

    name: str


@dataclass(frozen=True)
class LegacyListLink:
    """The link names a number without a unique List keeper."""

    number: int


type ListLink = investigation.ListDestination | LegacyListLink


@dataclass(frozen=True)
class ExchangeAnswer:
    text: str
    time: datetime | TimeNotRecorded
    read: AnswerRead


@dataclass(frozen=True)
class Exchange:
    source: Source
    source_key: str
    exchange_id: str
    direction: Direction
    question: str
    asked_at: datetime | TimeNotRecorded
    asker: staff.StaffId | Nate
    answerer: staff.StaffId | Nate | RecordedAnswerer
    answer: ExchangeAnswer | QuestionWithoutAnswer
    list_links: tuple[ListLink, ...]


@dataclass(frozen=True)
class ExchangeInventory:
    exchanges: tuple[Exchange, ...]
    list_items: frozenset[investigation.ListDestination]


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


def _question_exchanges(record: update_registration.QuestionRecord, source: Source, key: str,
                        links: tuple[ListLink, ...], zone: ZoneInfo) -> Iterator[Exchange]:
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
        yield Exchange(source, key, f"followup:{index + 1}", Direction.ANSWER_TO_NATE, reply["followup"],
                       asked_at, Nate.USER, record.owner, answer, links)


@dataclass(frozen=True)
class _LoggedQuestion:
    identity: str
    text: str
    time: datetime


def _scoped_log_exchanges(item: review_items.ReviewItem, key: str, links: tuple[ListLink, ...],
                          zone: ZoneInfo) -> Iterator[Exchange]:
    """Require a question before accepting a scoped reply from a non-Question record."""
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
    elif isinstance(source, review_items.ProposalSource):
        events = proposal_issue.review_log(source.record)
        extra_reads = ()
    else:
        return
    log = tuple(ReviewLog(review_items.aware(event.time, zone), event.who, event.words, event.feedback_request_id)
                for event in events)
    read_times = (*_read_times(log), *extra_reads)
    for index, event in enumerate(log):
        action, _, words = event.words.partition(": ")
        if event.who == Nate.USER and action in ("question", "followup") and words.strip():
            pending = _LoggedQuestion(f"log:{index}", words, event.time)
        elif event.who == Nate.USER and action in ("answer", "Approve", "Decline", "feedback sent", "Marked read", "read"):
            pending = None
        elif action == "Answer" and event.who != Nate.USER and words.strip() and pending is not None:
            state = _read_state(event.time, read_times, acknowledged=item.state is review_items.ReviewState.DONE)
            answer = ExchangeAnswer(words, event.time, state)
            yield Exchange(Source.SCOPED_REPLY, key, pending.identity, Direction.ANSWER_TO_NATE, pending.text,
                           pending.time, Nate.USER, item.owner, answer, links)
            pending = None


def _review_exchanges(root: Path, members: staff.StaffSnapshot, links: dict[str, list[ListLink]],
                      zone: ZoneInfo) -> Iterator[Exchange]:
    kinds = (*review_items.QUESTION_TYPES, *review_items.INVESTIGATION_TYPES, review_items.ReviewType.PROPOSAL)
    for item in review_items.read(root, members=members, kinds=kinds, zone=zone):
        source = item.source
        origin = review_items.submission_origin(item)
        destinations = list(links.get(item.key, ()))
        if (isinstance(source, review_items.InvestigationSource)
                and isinstance(destination := source.record.list_destination, investigation.ListDestination)
                and destination not in destinations):
            destinations.append(destination)
        if isinstance(origin, new_item_dialog.ReviewSubmission):
            kind = ("question" if isinstance(source, (review_items.QuestionSource, review_items.UnownedQuestionSource))
                    else source.record.kind.value if isinstance(source, review_items.InvestigationSource) else "proposal")
            key = f"submission/{kind}/{origin.request_id}"
            if isinstance(source, (review_items.QuestionSource, review_items.UnownedQuestionSource)):
                yield from _question_exchanges(source.record, Source.SCOPED_REPLY, key, tuple(destinations), zone)
            else:
                yield from _scoped_log_exchanges(item, key, tuple(destinations), zone)
        elif isinstance(source, (review_items.QuestionSource, review_items.UnownedQuestionSource)):
            yield from _question_exchanges(source.record, Source.STAFF_QUESTION, item.key, tuple(destinations), zone)


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


def _picture_exchanges(root: Path, keepers: dict[int, list[staff.StaffId]], zone: ZoneInfo) -> Iterator[Exchange]:
    directory = images.images_root(root)
    for record in images.records(root):
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
    exchanges = [
        *_review_exchanges(root, members, links, zone),
        *(exchange for entry in entries for exchange in _list_exchanges(entry, members, zone)),
        *_picture_exchanges(root, keepers, zone),
    ]
    exchanges.sort(key=lambda exchange: (exchange.source, exchange.source_key, exchange.exchange_id))
    return ExchangeInventory(tuple(exchanges), frozenset(list_items))


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("command", choices=("count",))
    _ = parser.parse_args()
    for line in _count(read_exchanges(change_routing.review_root(), members=staff.snapshot())):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

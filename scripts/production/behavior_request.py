"""Read Nate's words from typed turns and saved records."""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
import json
from pathlib import Path
import re
import subprocess
from typing import cast

from scripts.agents import catch_up
from scripts.lib import transcripts
from scripts.lib.showrunner_state import state_root
from scripts.production import record_store, staff, the_list, update_registration
from scripts.review_links import review_events


@dataclass(frozen=True)
class NateWords:
    text: str
    time: datetime


# Keep initials, common abbreviations, and ellipses within a sentence.
SENTENCE_END = re.compile(
    r"(?:(?<=[!?])|(?<=\.)(?<!\.\.)(?<!\b[a-z]\.)(?<!\b(?:mr|ms|dr|st|vs)\.)(?<!\b(?:mrs|etc)\.))\s+",
    re.IGNORECASE,
)


def complete_quotes(text: str, normalized_quotes: dict[str, str]) -> Iterator[str]:
    """Match quotes across consecutive lines or sentences."""
    segments = [segment for line in text.splitlines() for part in SENTENCE_END.split(line)
                if (segment := " ".join(part.split()))]
    starts: list[int] = []
    ends: set[int] = set()
    start = 0
    for segment in segments:
        starts.append(start)
        end = start + len(segment)
        ends.add(end)
        start = end + 1
    words = " ".join(segments)
    for original, quote in normalized_quotes.items():
        if any(start + len(quote) in ends and words.startswith(quote, start) for start in starts):
            yield original


def _has_excluded_turn_markers(fields: dict[str, object]) -> bool:
    return (any(fields.get(key) for key in ("isSidechain", "isMeta", "isCompactSummary",
                                           "isSynthetic", "isVisibleInTranscriptOnly"))
            or not fields.keys().isdisjoint({"source", "sourceToolAssistantUUID", "sourceToolUseID", "toolUseResult",
                                            "turnCompanion", "pastedContents"}))


def typed_turn(row: object) -> Iterator[NateWords]:
    if not isinstance(row, dict):
        return
    fields = cast(dict[str, object], row)
    row_type = fields.get("type")
    if row_type == "user":
        if (fields.get("origin") != {"kind": "human"} or fields.get("turnOrigin") != "human"
                or fields.get("promptSource") not in ("typed", "queued")):
            return
        message = fields.get("message")
        if not isinstance(message, dict):
            return
        message = cast(dict[str, object], message)
        if message.get("role") != "user" or "source" in message:
            return
        content, stamp = message.get("content"), fields.get("timestamp")
    elif row_type == "attachment":
        attachment = fields.get("attachment")
        if not isinstance(attachment, dict):
            return
        attachment = cast(dict[str, object], attachment)
        if (attachment.get("type") != "queued_command" or attachment.get("origin") != {"kind": "human"}
                or attachment.get("humanTurn") is not True or attachment.get("commandMode") != "prompt"
                or _has_excluded_turn_markers(attachment)):
            return
        content, stamp = attachment.get("prompt"), attachment.get("timestamp")
    else:
        return
    if not isinstance(stamp, str) or _has_excluded_turn_markers(fields):
        return
    if isinstance(content, str):
        words = content
    else:
        if not isinstance(content, list):
            return
        parts: list[str] = []
        for part in cast(list[object], content):
            if not isinstance(part, dict):
                return
            part_fields = cast(dict[str, object], part)
            match part_fields:
                case {"type": "text", "text": str(text)} if len(part_fields) == 2:
                    parts.append(text)
                case {"type": "image", "source": dict()} if row_type == "user" and len(part_fields) == 2:
                    continue
                case _:
                    return
        words = "\n".join(parts)
    if (re.search(rf"<(?:pasted_content|{catch_up.GENERATED_TAGS})(?:\s|>)", words, flags=re.I)
            or words.lstrip().startswith(catch_up.GENERATED_PREFIXES)):
        return
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return
    if moment.utcoffset() is not None:
        yield NateWords(words, moment)


def transcript_words(path: Path) -> Iterator[NateWords]:
    source = transcripts.transcript_source(path).resolve()
    if not any(source.parent.parent == (config / "projects").resolve() for config in transcripts.config_dirs()):
        return
    if not source.is_file():
        return
    with transcripts.open_transcript(source) as stream:
        for line in stream:
            try:
                row = cast(object, json.loads(line))
            except (ValueError, UnicodeError):
                continue
            yield from typed_turn(row)


def list_words(path: Path, keeper: staff.StaffId) -> Iterator[NateWords]:
    item = the_list.load(keeper, path.stem)
    for event in item.log:
        feedback = event.what
        if (event.source == the_list.PAGE_SOURCE and isinstance(feedback, the_list.PageFeedback)
                and feedback.action == "tell" and feedback.state == "sent"):
            yield NateWords(feedback.words, event.time)
    for question in item.questions:
        answer = question.answer
        if answer is None:
            continue
        events = [event for event in item.log if isinstance(event.what, the_list.QuestionAnswered)
                  and event.what.question_id == question.question_id]
        if len(events) == 1 and events[0].source == "nate":
            yield NateWords(answer.text, datetime.fromisoformat(answer.answered_at))


def question_answers(path: Path, legacy_owner: str) -> Iterator[NateWords]:
    question = update_registration.read_question_record(path, legacy_owner)
    if not question.owner:
        return
    folders = record_store.known_folders(record_store.RecordKind.QUESTION, question.owner,
                                         strict=True, include_history=False)
    if path.parent not in {folder.resolve() for folder in (*folders, *record_store.archive_folders(folders))}:
        return
    events = review_events.decode(question.item.get("review_log", ""))
    for event in events:
        label, _, words = event.words.partition(": ")
        if event.who == "nate" and label.casefold() in ("answer", "feedback sent"):
            yield NateWords(words, event.time)


def source_words(path: Path) -> Iterator[NateWords]:
    if transcripts.uncompressed_path(path).suffix == ".jsonl":
        yield from transcript_words(path)
        return
    if path.suffix != ".md" or path.is_symlink() or not path.is_file():
        return
    source = path.resolve()
    root = state_root().resolve()
    if source.is_relative_to(root):
        parts = source.relative_to(root).parts
        if len(parts) == 3 and parts[1] == "list" and re.fullmatch(r"L[1-9][0-9]*", source.stem):
            yield from list_words(source, staff.StaffId(parts[0]))
            return
    if source.name.startswith("question-"):
        owner = source.parent.parent.name if source.parent.parent.parent == root else ""
        yield from question_answers(source, owner)


def verified_dates(path: Path, quotes: set[str]) -> dict[str, date]:
    """Return the source date of the latest match for each complete quote."""
    normalized = {quote: " ".join(quote.split()) for quote in quotes}
    found: dict[str, datetime] = {}
    try:
        for words in source_words(path):
            if words.time.utcoffset() is None:
                continue
            for quote in complete_quotes(words.text, normalized):
                if quote not in found or words.time > found[quote]:
                    found[quote] = words.time
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError, subprocess.CalledProcessError,
            update_registration.InputError, update_registration.RegistrationFailure):
        return {}
    return {quote: moment.date() for quote, moment in found.items()}

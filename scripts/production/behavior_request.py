"""Read Nate's words from typed turns and saved answers."""
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


# Treat short words and abbreviations before a period as ambiguous boundaries.
SENTENCE_END = re.compile(
    r"(?:(?<!\b[A-Za-z])(?<!\b[A-Za-z]{2})(?<!\b[A-Za-z]{3})(?<!\.)\.|[!?])(?=\s+[A-Z])"
)


def quoted_words(text: str) -> Iterator[str]:
    for line in text.splitlines():
        yield line.strip()
        start = 0
        for end in SENTENCE_END.finditer(line):
            yield line[start:end.end()].strip()
            start = end.end()
        if start and line.rstrip().endswith((".", "!", "?")):
            yield line[start:].strip()


def typed_turn(row: object) -> Iterator[NateWords]:
    if not isinstance(row, dict):
        return
    fields = cast(dict[str, object], row)
    if (fields.get("type") != "user" or fields.get("origin") != {"kind": "human"}
            or fields.get("turnOrigin") != "human" or fields.get("promptSource") != "typed"
            or any(fields.get(key) for key in ("isSidechain", "isMeta", "isCompactSummary",
                                               "isSynthetic", "isVisibleInTranscriptOnly"))
            or fields.keys() & {"source", "sourceToolAssistantUUID", "sourceToolUseID", "toolUseResult",
                                "turnCompanion", "pastedContents"}):
        return
    message = fields.get("message")
    stamp = fields.get("timestamp")
    if not isinstance(message, dict) or not isinstance(stamp, str):
        return
    message = cast(dict[str, object], message)
    if message.get("role") != "user" or "source" in message:
        return
    content = message.get("content")
    if isinstance(content, str):
        words = content
    else:
        if not isinstance(content, list):
            return
        parts: list[str] = []
        for part in cast(list[object], content):
            if not isinstance(part, dict):
                return
            fields = cast(dict[str, object], part)
            match fields:
                case {"type": "text", "text": str(text)} if len(fields) == 2:
                    parts.append(text)
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


def list_answers(path: Path, keeper: staff.StaffId) -> Iterator[NateWords]:
    item = the_list.load(keeper, path.stem)
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
        if event.who == "nate" and label.casefold() == "answer":
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
            yield from list_answers(source, staff.StaffId(parts[0]))
            return
    if source.name.startswith("question-"):
        owner = source.parent.parent.name if source.parent.parent.parent == root else ""
        yield from question_answers(source, owner)


def verified_dates(path: Path, quotes: set[str]) -> dict[str, date]:
    """Return the source date of the latest match for each complete quote."""
    found: dict[str, datetime] = {}
    try:
        for words in source_words(path):
            if words.time.utcoffset() is None:
                continue
            for quote in quotes.intersection(quoted_words(words.text)):
                if quote not in found or words.time > found[quote]:
                    found[quote] = words.time
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError, subprocess.CalledProcessError,
            update_registration.InputError, update_registration.RegistrationFailure):
        return {}
    return {quote: moment.date() for quote, moment in found.items()}

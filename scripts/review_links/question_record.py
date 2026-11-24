"""Question records and their JSON form."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import NotRequired, TypedDict, cast

from scripts.production.staff import StaffId


@dataclass(frozen=True)
class Answer:
    text: str
    answered_at: str


class DefaultQuestionRecipient(Enum):
    """Use the page's recipient when the question has no saved recipient."""

    PAGE = "page"


@dataclass(frozen=True)
class KeeperForUnavailableProducer:
    keeper: StaffId
    producer: StaffId


QuestionRecipient = StaffId | DefaultQuestionRecipient | KeeperForUnavailableProducer


@dataclass(frozen=True)
class Question:
    """A missing answer means that the recipient must answer this question."""

    question_id: str
    asked_at: str
    words: str
    answer: Answer | None = None
    recipient: QuestionRecipient = DefaultQuestionRecipient.PAGE

    def recipient_id(self, default: StaffId) -> StaffId:
        if isinstance(self.recipient, KeeperForUnavailableProducer):
            return self.recipient.keeper
        return default if self.recipient is DefaultQuestionRecipient.PAGE else self.recipient


class AnswerRefused(ValueError):
    """An invalid answer must not change the review record."""


class StoredAnswer(TypedDict):
    text: str
    answered_at: str


class StoredQuestionRecipient(TypedDict):
    recipient: NotRequired[StaffId]
    unavailable_producer: NotRequired[StaffId]


class StoredQuestion(StoredQuestionRecipient):
    question_id: str
    asked_at: str
    words: str
    answer: StoredAnswer | None


def recipient_fields(recipient: QuestionRecipient) -> StoredQuestionRecipient:
    if isinstance(recipient, KeeperForUnavailableProducer):
        return {'recipient': recipient.keeper, 'unavailable_producer': recipient.producer}
    return {} if recipient is DefaultQuestionRecipient.PAGE else {'recipient': recipient}


def stored(question: Question) -> StoredQuestion:
    answer = question.answer
    return {
        'question_id': question.question_id, 'asked_at': question.asked_at, 'words': question.words,
        'answer': None if answer is None else {'text': answer.text, 'answered_at': answer.answered_at},
        **recipient_fields(question.recipient),
    }


def _staff_id(value: object) -> StaffId:
    if not isinstance(value, str) or not value or Path(value).name != value or value in ('.', '..') or '\x00' in value:
        raise ValueError('invalid question staff id')
    return StaffId(value)


def read_recipient(fields: dict[str, object]) -> QuestionRecipient:
    if 'unavailable_producer' in fields:
        return KeeperForUnavailableProducer(_staff_id(fields.get('recipient')),
                                            _staff_id(fields['unavailable_producer']))
    return _staff_id(fields['recipient']) if 'recipient' in fields else DefaultQuestionRecipient.PAGE


def read(value: object) -> Question:
    """Check a stored question before it enters the page's domain."""
    if not isinstance(value, dict):
        raise ValueError('invalid stored question')
    fields = cast(dict[str, object], value)
    identity, asked_at, words = (fields.get(key) for key in ('question_id', 'asked_at', 'words'))
    if not isinstance(identity, str) or re.fullmatch(r'Q[1-9][0-9]*', identity) is None or not isinstance(
            asked_at, str) or not isinstance(words, str) or not words.strip() or 'answer' not in fields:
        raise ValueError('invalid stored question')
    _ = datetime.fromisoformat(asked_at)
    recipient = read_recipient(fields)
    value = fields['answer']
    if value is None:
        return Question(identity, asked_at, words, recipient=recipient)
    if not isinstance(value, dict):
        raise ValueError('invalid stored answer')
    answer = cast(dict[str, object], value)
    text, answered_at = answer.get('text'), answer.get('answered_at')
    if not isinstance(text, str) or not text.strip() or not isinstance(answered_at, str):
        raise ValueError('invalid stored answer')
    _ = datetime.fromisoformat(answered_at)
    return Question(identity, asked_at, words, Answer(text, answered_at), recipient)

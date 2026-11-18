"""Question records and their JSON form."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import TypedDict, cast


@dataclass(frozen=True)
class Answer:
    text: str
    answered_at: str


@dataclass(frozen=True)
class Question:
    """A missing answer means that the owner must answer this question."""

    question_id: str
    asked_at: str
    words: str
    answer: Answer | None = None


class AnswerRefused(ValueError):
    """An invalid answer must not change the review record."""


class StoredAnswer(TypedDict):
    text: str
    answered_at: str


class StoredQuestion(TypedDict):
    question_id: str
    asked_at: str
    words: str
    answer: StoredAnswer | None


def stored(question: Question) -> StoredQuestion:
    answer = question.answer
    return {'question_id': question.question_id, 'asked_at': question.asked_at, 'words': question.words,
            'answer': None if answer is None else {'text': answer.text, 'answered_at': answer.answered_at}}


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
    value = fields['answer']
    if value is None:
        return Question(identity, asked_at, words)
    if not isinstance(value, dict):
        raise ValueError('invalid stored answer')
    answer = cast(dict[str, object], value)
    text, answered_at = answer.get('text'), answer.get('answered_at')
    if not isinstance(text, str) or not text.strip() or not isinstance(answered_at, str):
        raise ValueError('invalid stored answer')
    _ = datetime.fromisoformat(answered_at)
    return Question(identity, asked_at, words, Answer(text, answered_at))

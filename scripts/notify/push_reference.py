"""Identify the question that a kept push concerns."""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NotRequired, TypedDict, cast

from scripts.production import record_store


class KeptPushDropped(int):
    """A successful release removed a push without phone delivery."""

    def __new__(cls) -> KeptPushDropped:
        return super().__new__(cls, 0)


class QuestionReference(TypedDict):
    kind: Literal["question"]
    path: str
    since: str
    machine: NotRequired[Literal["mac"]]


class NoReference(TypedDict):
    kind: Literal["none"]


PushReference = QuestionReference | NoReference
NO_REFERENCE: NoReference = {"kind": "none"}


def read_reference(value: object) -> PushReference:
    if value is None:
        return NO_REFERENCE
    if isinstance(value, dict):
        record = cast(dict[str, object], value)
        if record.get("kind") == "none":
            return NO_REFERENCE
        path, since = record.get("path"), record.get("since")
        if record.get("kind") == "question" and isinstance(path, str) and Path(path).is_absolute() and isinstance(since, str) and since:
            result = QuestionReference(kind="question", path=path, since=since)
            if "machine" in record:
                if record["machine"] != "mac":
                    raise ValueError("invalid question host")
                result["machine"] = "mac"
            return result
    raise ValueError("invalid push reference")


@dataclass(frozen=True)
class QuestionAddress:
    machine: Literal["natedev", "mac"]
    path: str
    since: str

    @property
    def reference(self) -> QuestionReference:
        result = QuestionReference(kind="question", path=self.path, since=self.since)
        if self.machine == "mac":
            result["machine"] = "mac"
        return result


@dataclass(frozen=True, init=False)
class PushIdentity:
    sender: str
    key: str
    question: QuestionAddress | None

    def __init__(self, sender: str, key: str, reference: PushReference = NO_REFERENCE) -> None:
        object.__setattr__(self, "sender", sender)
        object.__setattr__(self, "key", key)
        object.__setattr__(self, "question", QuestionAddress(reference.get("machine", "natedev"),
            reference["path"], reference["since"]) if reference["kind"] == "question" else None)

    @property
    def tag_key(self) -> str:
        """Preserve the tag encoding used by existing posts."""
        return self.key if self.question is None else json.dumps(
            [self.sender, self.key, self.question.reference], sort_keys=True)

    @property
    def state_slot(self) -> str:
        """Share the unbound window for a key, as with its Pushover tag."""
        if self.question is None:
            return "user-push:unbound"
        return "user-push:" + json.dumps([self.sender, self.question.reference], sort_keys=True)


@dataclass(frozen=True)
class QuestionOpening:
    status: str
    since: str


def read_question(path: Path) -> QuestionOpening:
    """Read committed Question frontmatter without waiting for a writer."""
    with record_store.record_number_lock(record_store.RecordKind.QUESTION, wait=False):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as error:
            raise ValueError(f"cannot read question record {path}: {error}") from error
    lines = text.splitlines()
    if not lines or lines[0] != "---" or "---" not in lines[1:]:
        raise ValueError(f"not a question record: {path}")
    fields: dict[str, str] = {}
    for line in lines[1:lines.index("---", 1)]:
        key, separator, value = line.partition(":")
        if separator:
            fields[key.strip()] = value.strip()
    if fields.get("record_type") != "question":
        raise ValueError(f"not a question record: {path}")
    registration = cast(object, json.loads(fields.get("registration", "null")))
    since = cast(dict[str, object], registration).get("since") if isinstance(registration, dict) else None
    if not isinstance(since, str) or not since:
        raise ValueError(f"question has no registration since: {path}")
    status = fields.get("status", "")
    if status not in ("open", "closed"):
        raise ValueError(f"invalid question status: {path}")
    return QuestionOpening(status, since)


def admission_reference(text_id: str, question: str = "", forwarded: str = "", *,
                        remote_forward: bool = False) -> PushReference:
    """Read local questions before admission. Preserve missing remote references."""
    reference: PushReference
    if forwarded:
        reference = read_reference(cast(object, json.loads(forwarded)))
    else:
        reference = NO_REFERENCE
    if question and reference["kind"] != "question":
        path = Path(question).expanduser().resolve()
        opening = read_question(path)
        if opening.status != "open":
            raise ValueError(f"question is {opening.status}: {path}")
        reference = QuestionReference(kind="question", path=str(path), since=opening.since)
        if sys.platform == "darwin":
            reference["machine"] = "mac"
    elif reference["kind"] == "question":
        path = Path(reference["path"])
        foreign = reference.get("machine") == "mac" and sys.platform != "darwin"
        if not (remote_forward and (foreign or not path.exists())):
            opening = read_question(path)
            if opening.status != "open":
                raise ValueError(f"question closed: {path}")
            if opening.since != reference["since"]:
                raise ValueError(f"question reopened since the push was made: {path}")
    if text_id == "decision.choices" and reference["kind"] != "question":
        raise ValueError("decision.choices requires --question with an open question record")
    return reference

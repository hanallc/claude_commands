"""Store private reply proofs and staff question request IDs."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import NewType, cast

from scripts.lib import atomic_file
from scripts.lib.file_lock import exclusive
from scripts.lib.showrunner_state import state_root
from scripts.review_links import reply_tokens


StaffQuestionRequestKey = NewType("StaffQuestionRequestKey", str)
ReplyWaitRevision = NewType("ReplyWaitRevision", str)
INITIAL_REPLY_WAIT = ReplyWaitRevision("")


@dataclass(frozen=True)
class NoStaffQuestionRequest:
    """The question was filed without a request key."""


NO_STAFF_REQUEST = NoStaffQuestionRequest()


@dataclass(frozen=True)
class SubmissionIdentity:
    """A Review item keeps this identity when it moves to another folder or owner."""

    number: str
    request_id: str
    revision: ReplyWaitRevision = INITIAL_REPLY_WAIT


@dataclass(frozen=True)
class StaffQuestionIdentity:
    """A staff question keeps its number within the owner's records."""

    owner: str
    number: str


type NoteIdentity = SubmissionIdentity | StaffQuestionIdentity


@dataclass(frozen=True)
class NoteProof:
    reply: reply_tokens.ReplyProof = reply_tokens.MISSING_REPLY_PROOF
    request: StaffQuestionRequestKey | NoStaffQuestionRequest = field(default=NO_STAFF_REQUEST, repr=False)


SUBMISSION_FIELDS = ("reply_token", "reply_token_sha256", "reply_token_nonce")


def folder() -> Path:
    return state_root() / "review-reply-proofs"


def path_for(identity: NoteIdentity) -> Path:
    match identity:
        case SubmissionIdentity(number, request_id, revision):
            parts = ("submission", number, request_id)
            if revision != INITIAL_REPLY_WAIT:
                parts = (*parts, revision)
        case StaffQuestionIdentity(owner, number):
            parts = ("staff-question", owner, number)
    name = hashlib.sha256(json.dumps(parts).encode()).hexdigest()
    return folder() / f"{name}.json"


def _proof(value: object, nonce: object) -> reply_tokens.StoredReplyProof:
    if not isinstance(value, str) or not reply_tokens.valid_hash(value):
        raise ValueError("Invalid saved reply hash.")
    digest = reply_tokens.SavedReplyHash(value)
    if nonce == "":
        return reply_tokens.LegacyReplyProof(digest)
    if not isinstance(nonce, str) or not reply_tokens.valid_nonce(nonce):
        raise ValueError("Invalid saved reply nonce.")
    return reply_tokens.ReplyTokenProof(nonce, digest)


def _request(value: object) -> StaffQuestionRequestKey:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Invalid staff question request key.")
    return StaffQuestionRequestKey(value)


def _read(path: Path) -> NoteProof:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return NoteProof()
    try:
        value = cast(object, json.loads(raw))
        if not isinstance(value, dict):
            raise ValueError
        row = cast(dict[str, object], value)
        reply = (_proof(row["sha256"], row.get("nonce", "")) if "sha256" in row
                 else reply_tokens.MISSING_REPLY_PROOF)
        request = _request(row["request"]) if "request" in row else NO_STAFF_REQUEST
        return NoteProof(reply, request)
    except ValueError:
        raise OSError("Cannot read a record in the Review reply proof store.") from None


def read(identity: NoteIdentity) -> NoteProof:
    path = path_for(identity)
    if not path.exists():
        return NoteProof()
    with exclusive(path.with_suffix(".lock")):
        return _read(path)


def _write(path: Path, proof: NoteProof) -> None:
    fields: dict[str, str] = {}
    if isinstance(proof.reply, (reply_tokens.ReplyTokenProof, reply_tokens.LegacyReplyProof)):
        fields["sha256"] = proof.reply.sha256
    if isinstance(proof.reply, reply_tokens.ReplyTokenProof):
        fields["nonce"] = proof.reply.nonce
    if not isinstance(proof.request, NoStaffQuestionRequest):
        fields["request"] = proof.request
    if fields:
        atomic_file.write_locked_atomic(path, (json.dumps(fields) + "\n").encode(), mode=0o600)
    else:
        path.unlink(missing_ok=True)
        atomic_file.sync_folder(path.parent)


def _prepare_folder(path: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_file.sync_folder(path.parent.parent)


def stage_wait(identity: SubmissionIdentity, proof: reply_tokens.ReplyTokenProof) -> None:
    """Save the next proof before the note selects its revision."""
    if identity.revision == INITIAL_REPLY_WAIT:
        raise ValueError("A new wait must advance the reply revision.")
    path = path_for(identity)
    _prepare_folder(path)
    with exclusive(path.with_suffix(".lock")):
        _write(path, NoteProof(reply=proof))


@contextmanager
def publishing(identity: NoteIdentity, proof: NoteProof, *, moving: bool = False) -> Generator[None]:
    """Save the proof before the note changes. Hold the note's lock on entry."""
    path = path_for(identity)
    _prepare_folder(path)
    with exclusive(path.with_suffix(".lock")):
        previous = _read(path)
        if moving and previous not in (NoteProof(), proof):
            raise ValueError("The note and the saved proof differ.")
        if previous == proof:
            if moving:
                atomic_file.sync_folder(path.parent)
            yield
            return
        _write(path, proof)
        try:
            yield
        except Exception:
            if not moving:
                _write(path, previous)
            raise


def header_has_legacy_submission(value: str) -> bool:
    """Find legacy field names in inline JSON or quoted JSON."""
    return re.search(r'\breply_token(?:_sha256|_nonce)?\\?"\s*:', value) is not None


def has_legacy_submission(fields: dict[str, object]) -> bool:
    return any(name in fields for name in SUBMISSION_FIELDS)


def extract_submission(fields: dict[str, object]
                       ) -> tuple[dict[str, object], reply_tokens.ReplyProof, int]:
    """Convert proof fields in a note during the move."""
    count = sum(name in fields for name in SUBMISSION_FIELDS)
    if not count:
        return fields, reply_tokens.MISSING_REPLY_PROOF, 0
    digest = fields.get("reply_token_sha256")
    raw = fields.get("reply_token")
    if digest is None and isinstance(raw, str) and reply_tokens.valid(raw):
        digest = reply_tokens.digest(reply_tokens.RawReplyToken(raw))
    proof = _proof(digest, fields.get("reply_token_nonce", ""))
    return {name: value for name, value in fields.items() if name not in SUBMISSION_FIELDS}, proof, count


def legacy_request(value: object) -> NoteProof:
    """Convert a staff question's request key during the move."""
    return NoteProof(request=_request(value))

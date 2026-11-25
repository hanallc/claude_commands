"""Store investigations by fixed staff owner and record their answers."""
from __future__ import annotations

try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import json
import re
import sys
from collections.abc import Callable, Generator, Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import cast, override

from scripts.console_text import check as console_text

from scripts.lib import atomic_file
from scripts.lib.user_zone import user_zone
from scripts.production import change_routing, delivery_state, record_store, staff
from scripts.production.issues import Issue
from scripts.review_links import new_item_dialog, reply_tokens, review_events
from scripts.review_links.review_events import ReviewLog
from scripts.production.record_store import state_root as state_root

ANSWER_SECTION = record_store.section("Answer")


class InvestigationError(ValueError):
    """An investigation cannot accept the requested operation."""


class InvestigationState(StrEnum):
    OPEN = "open"
    ANSWER_READY = "answer ready"
    READ = "read"


class InvestigationKind(StrEnum):
    INVESTIGATION = "investigation"
    BUG = "bug"


@dataclass(frozen=True)
class Investigation:
    id: str
    owner: staff.StaffId
    title: str
    state: InvestigationState
    filed: str
    answered: str
    read: str
    question: str
    answer: str
    log: tuple[ReviewLog, ...] = ()
    waiting_why: str = ""
    kind: InvestigationKind = InvestigationKind.INVESTIGATION
    submission: new_item_dialog.SubmissionOrigin = new_item_dialog.NoReviewSubmission()

    @property
    def source_owner(self) -> staff.StaffId:
        if isinstance(self.submission, new_item_dialog.ReviewSubmission):
            return new_item_dialog.request_recipient(new_item_dialog.SubmissionKind(self.kind.value),
                                                     self.submission.request_id)
        return self.owner


_NO_REVIEW_SUBMISSION = new_item_dialog.NoReviewSubmission()
_held_recipients: ContextVar[frozenset[Path]] = ContextVar("investigation_locks", default=frozenset())


@dataclass(frozen=True)
class Investigations(record_store.NumberedRecords):
    @override
    def lock_path(self) -> Path:
        # Keep the lock when cleanup removes the owner's directory.
        return state_root() / "investigation-locks" / (self.owner + ".lock")

    @override
    @contextmanager
    def locked(self, *, check: Callable[[], object] | None = None) -> Generator[None]:
        path = self.lock_path().resolve()
        held = _held_recipients.get()
        if path in held:
            if check is not None:
                _ = check()
            yield
            return
        with super().locked():
            token = _held_recipients.set(held | {path})
            try:
                if check is not None:
                    _ = check()
                yield
            finally:
                _held_recipients.reset(token)


def _store(owner: staff.StaffId) -> Investigations:
    store = Investigations(owner, "investigations", "I", "investigation", InvestigationError)
    _ = store.folder()
    return store


@contextmanager
def recipient_locks(*owners: staff.StaffId) -> Generator[None]:
    """Lock the native stores in a fixed order."""
    with ExitStack() as locks:
        for owner in sorted(set(owners)):
            _ = locks.enter_context(_store(owner).locked())
        yield


def owner_chain(owner: staff.StaffId, members: staff.StaffSnapshot) -> tuple[staff.StaffId, ...]:
    """Read the owner and each parent, including wrapped owners."""
    chain = [owner]
    row = members.by_id(owner)
    while row is not None:
        row = members.parent_of(row)
        if row is None or row.id in chain:
            break
        chain.append(row.id)
    return tuple(chain)


def folder(owner: staff.StaffId) -> Path:
    """Return the owner's investigation folder without creating it."""
    return _store(owner).folder()


def _record_id(path: Path, *, strict: bool = False) -> str:
    fields = record_store.read_header(path, keys=frozenset(("id", "owner")), strict=strict)
    issue = Issue(path, fields)
    identity = issue.value("id")
    return identity if re.fullmatch(r"I[1-9][0-9]*", identity) else ""


def _seed_id(path: Path) -> str:
    return _record_id(path, strict=True)


def investigation_numbers() -> AbstractContextManager[record_store.LockedRecordNumbers]:
    return record_store.record_numbers(record_store.RecordKind.INVESTIGATION, _seed_id)


def review_record_ids(owner: staff.StaffId, *, strict: bool = False) -> Iterator[tuple[Path, str]]:
    """Find native investigation records in the active store and archives."""
    roots = record_store.known_folders(record_store.RecordKind.INVESTIGATION, owner, strict=strict)
    for path in record_store.stored_paths(roots, strict=strict):
        if identity := _record_id(path, strict=strict):
            yield path, identity


def review_record_paths(owner: staff.StaffId, *, strict: bool = False) -> Iterator[Path]:
    """Find the paths of stored investigations."""
    yield from (path for path, _ in review_record_ids(owner, strict=strict))


def _record_path(store: Investigations, identity: str, *, strict: bool = True) -> Path:
    _ = store.path(identity)
    paths = [path for path, number in review_record_ids(store.owner, strict=strict) if number == identity]
    if len(paths) > 1:
        raise InvestigationError(f"{identity}: more than one investigation has this ID for owner {store.owner}")
    if not paths:
        raise InvestigationError(f"{identity}: no investigation for owner {store.owner}")
    return paths[0]


def load(owner: staff.StaffId, identity: str, *, sources: list[Path] | None = None) -> Investigation:
    """Read the investigation with this owner and stored ID."""
    store = _store(owner)
    return _load(store, identity, _record_path(store, identity, strict=False), sources=sources)


def review_owner(path: Path) -> staff.StaffId:
    path = path.resolve()
    state = state_root().resolve()
    if not path.is_relative_to(state):
        raise InvestigationError(f"{path}: not in an investigation store")
    parts = path.relative_to(state).parts
    if len(parts) < 3 or parts[1] not in ("investigations", "archive"):
        raise InvestigationError(f"{path}: not in an investigation store")
    return staff.StaffId(parts[0])


def read_review_record(path: Path) -> Investigation:
    store = _store(review_owner(path))
    identity = store.field(path.stem, "id", path=path)
    return _load(store, identity, path)


def open_review_investigations(owner: staff.StaffId) -> Iterator[tuple[Path, Investigation]]:
    """Find user filings that still need an answer or review."""
    for path, identity in review_record_ids(owner, strict=True):
        header = record_store.read_header(path, keys=frozenset(("state", "review_submission")), strict=True)
        if header.get("state") != [InvestigationState.READ] and header.get("review_submission"):
            yield path, _load(_store(owner), identity, path)


def find_review_submission(owner: staff.StaffId, kind: InvestigationKind, request_id: str
                           ) -> tuple[Path, Investigation] | None:
    """Find a request in one owner's native store, including its archive."""
    for path in review_record_paths(owner, strict=True):
        item = read_review_record(path)
        if (item.kind == kind and isinstance(item.submission, new_item_dialog.ReviewSubmission)
                and item.submission.request_id == request_id):
            return path, item
    return None


def _load(store: Investigations, identity: str, path: Path, *, sources: list[Path] | None = None) -> Investigation:
    if not store.valid_id(identity):
        raise InvestigationError(f"{identity}: invalid investigation ID")
    fields, body = store.read(identity, sources=sources, path=path)
    try:
        submission = new_item_dialog.decode_submission(fields.get("review_submission", ""))
        kind = InvestigationKind(fields.get("kind", "investigation"))
        header = Issue(path, {"owner": [fields["owner"]]} if "owner" in fields else {})
        recorded_owner = record_store.record_owner(record_store.RecordKind.INVESTIGATION, header, store.owner)
        if isinstance(submission, new_item_dialog.ReviewSubmission):
            _ = new_item_dialog.request_recipient(new_item_dialog.SubmissionKind(kind.value), submission.request_id)
        if recorded_owner != store.owner:
            raise ValueError("record identity differs from its path")
        question, answer = record_store.split_section(body, int(fields["question_length"]), "Answer")
        item = Investigation(fields["id"], store.owner, fields["title"],
                             InvestigationState(fields["state"]), fields["filed"], fields["answered"],
                             fields["read"], question, answer, review_events.decode(fields.get("review_log", "")),
                             review_events.waiting_why(fields.get("waiting_why", "")), kind,
                             submission)
        if not item.title.strip() or record_store.has_line_break(item.title):
            raise ValueError("title must have one line")
        for name, value, required in (
                ("filed", item.filed, True),
                ("answered", item.answered, item.state != InvestigationState.OPEN),
                ("read", item.read, item.state == InvestigationState.READ)):
            if required:
                if datetime.fromisoformat(value).utcoffset() is None:
                    raise ValueError(f"{name} must have a time zone")
            elif value:
                raise ValueError(f"{name} must be empty in state {item.state}")
        return item
    except (ValueError, KeyError) as error:
        raise InvestigationError(f"{identity}: invalid investigation: {error}") from None


def records(owner: staff.StaffId, *, include_read: bool = False,
            sources: list[Path] | None = None,
            states: tuple[InvestigationState, ...] = ()) -> list[Investigation]:
    """Read investigations in numeric order and watch the folder and each record."""
    selected = states or (() if include_read else (InvestigationState.OPEN, InvestigationState.ANSWER_READY))
    store = _store(owner)
    items: list[Investigation] = []
    roots = record_store.known_folders(record_store.RecordKind.INVESTIGATION, owner)
    for path in record_store.stored_paths(roots, sources=sources):
        if sources is not None:
            sources.append(path)
        if "archive" in path.parts and not _record_id(path):
            continue
        try:
            if not selected or store.field(path.stem, "state", path=path) in selected:
                items.append(read_review_record(path))
        except (OSError, ValueError, KeyError) as error:
            print(f"investigation: {path}: {str(error).replace(chr(10), ' ')}", file=sys.stderr)
    return sorted((item for item in items if include_read or item.state != InvestigationState.READ),
                  key=lambda item: int(item.id[1:]))


def _save(store: Investigations, item: Investigation, path: Path,
          numbers: record_store.LockedRecordNumbers | None = None, *, new_record: bool = False,
          before_write: Callable[[], None] = lambda: None) -> Investigation:
    fields = {"id": item.id, "owner": item.owner, "title": item.title, "state": item.state,
              "filed": item.filed, "answered": item.answered, "read": item.read,
              "question_length": str(len(item.question)), "review_log": review_events.encode(item.log),
              "waiting_why": item.waiting_why, "kind": item.kind}
    if isinstance(item.submission, new_item_dialog.ReviewSubmission):
        fields["review_submission"] = json.dumps(new_item_dialog.stored_submission(item.submission))
    body = f"{item.question}{ANSWER_SECTION}{item.answer}"
    with investigation_numbers() if numbers is None else nullcontext(numbers) as sequence:
        with sequence.write(path, item.id, store.owner, folder=store.folder(), new_record=new_record,
                            before_write=before_write):
            try:
                store.write(item.id, fields, body, path=path, new_record=new_record)
            except record_store.ReusedReviewRecord:
                return read_review_record(path)
    return item


def write_review_submission(path: Path, item: Investigation, details: new_item_dialog.ReviewSubmission) -> None:
    """Update the submission fields while the caller holds the record lock."""
    store = _store(item.owner)
    _write_submission_fields(store, item.id, path, new_item_dialog.stored_submission(details))


def _write_submission_fields(store: Investigations, identity: str, path: Path,
                             fields: dict[str, object]) -> None:
    """Replace the submission field without rewriting the rest of the note."""
    with path.open(encoding="utf-8", newline="") as stream:
        lines = record_store.lines_with_endings(stream.read())
    for index, line in enumerate(lines[1:], 1):
        if line.rstrip("\r\n") == "---":
            break
        if line.startswith("review_submission:"):
            newline = line[len(line.rstrip("\r\n")):]
            lines[index] = "review_submission: " + json.dumps(json.dumps(fields), ensure_ascii=False) + newline
            with investigation_numbers() as numbers, numbers.write(path, identity, store.owner, folder=store.folder()):
                atomic_file.write_text_atomic(path, "".join(lines))
            return
    raise InvestigationError(f"{path}: the Review submission field is missing")


def renew_reply_proof(path: Path, item: Investigation, proof: reply_tokens.ReplyTokenProof) -> None:
    """Replace the proof under the Investigation lock."""
    store = _store(item.owner)
    fields, _ = store.read(item.id, path=path)
    value = cast(dict[str, object], json.loads(fields["review_submission"]))
    _write_submission_fields(store, item.id, path, new_item_dialog.renewed_submission_fields(value, proof))


def scrub_reply_token(path: Path, owner: staff.StaffId, identity: str) -> bool:
    store = _store(owner)
    with store.locked():
        fields, _ = store.read(identity, path=path)
        raw = fields.get("review_submission", "")
        if not raw:
            return False
        value = cast(dict[str, object], json.loads(raw))
        details = new_item_dialog.read_submission(value)
        updated = new_item_dialog.hashed_submission_fields(value, details)
        if updated is value:
            return False
        _write_submission_fields(store, identity, path, updated)
        return True


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def file(owner: staff.StaffId, title: str, question: str, *, waiting_why: str = "",
         kind: InvestigationKind = InvestigationKind.INVESTIGATION,
         submission: new_item_dialog.SubmissionOrigin = _NO_REVIEW_SUBMISSION,
         before_write: Callable[[str, Path], None] = lambda _id, _path: None) -> Investigation:
    """File an Investigation with the next number on this machine."""
    if not title.strip() or record_store.has_line_break(title):
        raise InvestigationError("title must have one nonempty line")
    if not question.strip():
        raise InvestigationError("question must not be empty")
    reason = review_events.require_waiting_why(waiting_why)
    store = _store(owner)
    with store.locked(), investigation_numbers() as numbers:
        identity = numbers.next_id(store.folder())
        log = (ReviewLog(datetime.now(timezone.utc), "nate", "Sent"),) if isinstance(
            submission, new_item_dialog.ReviewSubmission) else ()
        names: set[str] = set()
        while True:
            item = Investigation(identity, owner, title, InvestigationState.OPEN, _now(), "", "", question, "",
                                 log, reason, kind, submission)
            path = record_store.free_record_path(store.path(identity), names)
            try:
                saved = _save(store, item, path, numbers, new_record=True,
                              before_write=lambda: before_write(identity, path))
            except record_store.OccupiedRecordPath:
                names.add(path.name)
                identity = numbers.next_id_after_collision(identity, path)
                continue
            if saved is not item:
                before_write(saved.id, path)
            return saved


def answer(owner: staff.StaffId, identity: str, text: str, *, question_id: str = '') -> Investigation:
    """Save an answer and refuse a stale Review exchange ID."""
    item, _ = _answer(_store(owner), identity, text, question_id)
    return item


@dataclass(frozen=True)
class SubmissionReply:
    key: new_item_dialog.ScopedSubmissionKey
    token: str


@dataclass(frozen=True)
class NativeReply:
    pass


_NATIVE_REPLY = NativeReply()

def answer_submission(path: Path, key: new_item_dialog.ScopedSubmissionKey, text: str, token: str, *,
                      owner: staff.StaffId) -> bool:
    _, changed = _answer(_store(owner), key.item_id, text, "", SubmissionReply(key, token), source=path)
    return changed


def _answer(store: Investigations, identity: str, text: str, question_id: str,
            reply: SubmissionReply | NativeReply = _NATIVE_REPLY, *, source: Path | None = None) -> tuple[Investigation, bool]:
    if not text.strip():
        raise InvestigationError("answer must not be empty")
    with store.locked():
        path = _record_path(store, identity) if source is None else source
        item = _load(store, identity, path)
        if isinstance(reply, SubmissionReply):
            key = reply.key
            if (item.kind.value != key.kind.value
                    or not isinstance(item.submission, new_item_dialog.ReviewSubmission)
                    or item.submission.request_id != key.request_id):
                raise InvestigationError("The selected investigation changed.")
            new_item_dialog.require_reply_token(item.submission, reply.token)
        else:
            if isinstance(item.submission, new_item_dialog.ReviewSubmission):
                raise InvestigationError("Use the scoped reply from the Review item.")
            delivery = change_routing.delivery_path(change_routing.review_root(), store.path(identity), identity)
            question = change_routing.delivery_slots(delivery).question
            if question_id or isinstance(question, delivery_state.OpenQuestion):
                try:
                    _ = delivery_state.question_exchange_for_reply(question, question_id, text.strip())
                except delivery_state.StaleQuestionExchange as error:
                    raise InvestigationError(str(error)) from None
        repeated = item.state == InvestigationState.ANSWER_READY and item.answer == text
        if item.state != InvestigationState.OPEN and not repeated:
            if isinstance(reply, SubmissionReply):
                raise InvestigationError("This item no longer accepts this reply.")
            raise InvestigationError(f"{identity}: cannot answer an investigation in state {item.state}")
        if repeated:
            return item, False
        exchange = reply.key.request_id if isinstance(reply, SubmissionReply) else question_id
        console_text.require(text, "investigation answer", identity=f"{path}/{exchange}",
                             refusal=InvestigationError)
        item = replace(item, state=InvestigationState.ANSWER_READY, answered=_now(), answer=text,
                       log=(*item.log, ReviewLog(datetime.now(timezone.utc), str(store.owner), "Answer: "+text)))
        _ = _save(store, item, path)
        return item, True


def mark_read(owner: staff.StaffId, identity: str, words: str = "", *,
              check: Callable[[], object] | None = None) -> Investigation:
    """Mark an answer read once; accept an exact repeat without writing."""
    store = _store(owner)
    with store.locked(check=check):
        path = _record_path(store, identity)
        item = _load(store, identity, path)
        if item.state == InvestigationState.READ:
            return item
        if item.state != InvestigationState.ANSWER_READY:
            raise InvestigationError(f"{identity}: cannot read an investigation in state {item.state}")
        item = replace(item, state=InvestigationState.READ, read=_now(),
                       log=(*item.log, ReviewLog(datetime.now(timezone.utc), "nate", "Marked read"+(': '+words if words else ''))))
        _ = _save(store, item, path)
        return item


def review_action(owner: staff.StaffId, identity: str, action: str, words: str, *,
                  new_reply_proof: reply_tokens.ReplyTokenProof | reply_tokens.MintReplyProof = reply_tokens.MINT_REPLY_PROOF,
                  check: Callable[[], object] | None = None) -> Investigation:
    """Record feedback or a question and keep each previous answer in the log."""
    if not words.strip():
        raise InvestigationError("enter your words")
    store = _store(owner)
    with store.locked(check=check):
        path = _record_path(store, identity)
        item = _load(store, identity, path)
        replace_wait = (item.state == InvestigationState.OPEN and action == 'question'
                        and isinstance(new_reply_proof, reply_tokens.ReplyTokenProof)
                        and isinstance(item.submission, new_item_dialog.ReviewSubmission))
        if item.state != InvestigationState.ANSWER_READY and not replace_wait:
            raise InvestigationError(f"{identity}: the answer is no longer ready")
        words_before = 'Answer: '+item.answer
        if item.answer and not any(entry.words == words_before for entry in item.log):
            previous = ReviewLog(datetime.fromisoformat(item.answered), str(owner), words_before)
            item = replace(item, log=tuple(sorted((*item.log, previous), key=lambda entry: entry.time)))
        event = ReviewLog(datetime.now(timezone.utc), 'nate', ('feedback sent' if action == 'feedback' else action)+': '+words)
        if action == 'question':
            submission = item.submission
            if isinstance(submission, new_item_dialog.ReviewSubmission):
                proof = new_reply_proof if isinstance(new_reply_proof, reply_tokens.ReplyTokenProof) else reply_tokens.mint()
                submission = submission.with_reply_proof(proof)
            item = replace(item,state=InvestigationState.OPEN,answered='',read='',log=(*item.log,event),
                           waiting_why=review_events.require_waiting_why(words),submission=submission)
        else:
            item = replace(item,state=InvestigationState.READ,read=_now(),log=(*item.log,event))
        _ = _save(store, item, path)
        return item


def record_review_log(owner: staff.StaffId, identity: str, words: str, *,
                      check: Callable[[], object] | None = None) -> None:
    store = _store(owner)
    with store.locked(check=check):
        path = _record_path(store, identity)
        item = _load(store, identity, path)
        event = ReviewLog(datetime.now(timezone.utc), 'nate', words)
        _ = _save(store, replace(item, log=review_events.append(item.log, event)), path)


def change_waiting(record: Investigation, action: str, words: str, *, check: Callable[[], object] | None = None) -> None:
    """Update a waiting investigation selected by its owner and ID."""
    store = _store(record.owner)
    with store.locked(check=check):
        path = _record_path(store, record.id)
        item = _load(store, record.id, path)
        if item.submission != record.submission or item.state != InvestigationState.OPEN:
            raise InvestigationError("The selected investigation changed.")
        now = datetime.now(user_zone())
        label = "Marked read" if action == "read" else "feedback sent" if action == "feedback" else action
        event = ReviewLog(now, "nate", label + (": " + words if words else ""))
        log = review_events.append(item.log, event)
        updated = (replace(item, log=log) if action == "question" else
                   replace(item, state=InvestigationState.READ, answered=item.answered or now.isoformat(),
                           read=now.isoformat(), log=log))
        _ = _save(store, updated, path)


def set_waiting_why(owner: staff.StaffId, identity: str, text: str) -> None:
    reason = review_events.require_waiting_why(text)
    store = _store(owner)
    with store.locked():
        path = _record_path(store, identity)
        item = _load(store, identity, path)
        if item.state != InvestigationState.OPEN:
            raise InvestigationError("The item is not Waiting.")
        _ = _save(store, replace(item, waiting_why=reason), path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    create = subcommands.add_parser("file")
    _ = create.add_argument("--title", required=True)
    _ = create.add_argument("--question", required=True)
    _ = create.add_argument("--waiting-why", default="")
    respond = subcommands.add_parser("answer")
    _ = respond.add_argument("--id", required=True)
    _ = respond.add_argument("--text", required=True)
    _ = respond.add_argument("--owner", default="")
    read = subcommands.add_parser("read")
    _ = read.add_argument("--id", required=True)
    _ = read.add_argument("--owner", default="")
    listing = subcommands.add_parser("list")
    _ = listing.add_argument("--all", action="store_true")
    args = parser.parse_args(argv)
    try:
        record = staff.snapshot()
        caller = record.by_current_session(staff.caller_identity())
        if caller is None:
            raise InvestigationError("the current session has no staff row")
        command = cast(str, args.command)
        owner = caller.id
        if command in ("answer", "read") and (reference := cast(str, args.owner)):
            target = record.by_id(staff.StaffId(reference))
            if target is None:
                raise InvestigationError("owner has no staff row")
            owner = target.id
        if command == "file":
            print(file(owner, cast(str, args.title), cast(str, args.question), waiting_why=cast(str, args.waiting_why)).id)
        elif command == "answer":
            _ = answer(owner, cast(str, args.id), cast(str, args.text))
        elif command == "read":
            _ = mark_read(owner, cast(str, args.id))
        else:
            for item in records(owner, include_read=cast(bool, args.all)):
                print(f"{item.id} | {item.state} | {item.title}")
        return 0
    except (ValueError, staff.StaffRefused, OSError, UnicodeError) as error:
        print(f"investigation: {str(error).replace(chr(10), ' ')}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

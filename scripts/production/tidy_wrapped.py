#!/usr/bin/env python3
"""Delete state for wrapped productions without active timers.

Closed questions remain in their question folders.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import json
import os
import re
import shutil
import stat
import sys
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast

from scripts.lib import atomic_file  # noqa: E402
from scripts.production import investigation, record_store, showrunners, staff, unit_lookup  # noqa: E402
from scripts.copies import copies  # noqa: E402
from scripts.production.production_doc import WRAPPED  # noqa: E402
from scripts.production.proposals_folder import issues_folder  # noqa: E402
from scripts.review_links import new_item_dialog  # noqa: E402

if TYPE_CHECKING:
    from scripts.production.update_registration import QuestionStaff

STATE_DIR = Path(os.environ.get("SHOWRUNNER_STATE_DIR") or Path.home() / ".local/state/showrunner")
NOTIFIER_STATE_DIR = Path(os.environ.get("NOTIFIER_STATE_DIR") or Path.home() / ".local/state/notifier")


class TidyRefused(Exception):
    pass


def owned_paths(owner: staff.StaffRow) -> tuple[Path, ...]:
    identity = owner.id
    return (STATE_DIR / "footers-off" / identity,
            STATE_DIR / "outstanding" / f"{identity}.answered.jsonl",
            STATE_DIR / "outstanding" / f"{identity}.history.jsonl",
            STATE_DIR / "outstanding" / f"{identity}.lock",
            STATE_DIR / "outstanding" / f"{identity}.json", STATE_DIR / "counts" / identity,
            STATE_DIR / "reports" / f"{identity}.json", STATE_DIR / identity)


def can_hold_questions(document: Path) -> bool:
    """Check for question storage before reading the staff record."""
    folder = issues_folder(document)
    return folder is not None or any(
        path.is_dir() for path in STATE_DIR.glob("*/questions")
    )


def hash_move_header(header: bytes, kind: new_item_dialog.SubmissionKind,
                     submission: new_item_dialog.ReviewSubmission) -> bytes:
    """Replace the token in the native submission field."""
    question = kind == new_item_dialog.SubmissionKind.QUESTION
    field_name = b"registration" if question else b"review_submission"
    match = re.search(rb"(?m)^" + field_name + rb": ([^\r\n]*)", header)
    if match is None:
        raise TidyRefused("The moving record has no submission field.")
    value = cast(object, json.loads(match[1]))
    registration: dict[str, object] = {}
    if question and isinstance(value, dict):
        registration = cast(dict[str, object], value)
        value = registration.get("review_submission")
    elif not question and isinstance(value, str):
        value = cast(object, json.loads(value))
    if not isinstance(value, dict):
        raise TidyRefused("The moving record has invalid submission fields.")
    fields = cast(dict[str, object], value)
    hashed = new_item_dialog.hashed_submission_fields(fields, submission)
    if hashed is fields:
        return header
    if question:
        registration["review_submission"] = hashed
        encoded = json.dumps(registration, ensure_ascii=False)
    else:
        encoded = json.dumps(json.dumps(hashed), ensure_ascii=False)
    return header[:match.start(1)] + encoded.encode() + header[match.end(1):]


@dataclass(frozen=True)
class ReviewRecordMove:
    """One user filing and its destination under the parent showrunner."""

    source: Path
    target: Path
    owner: staff.StaffId
    identity: str
    kind: new_item_dialog.SubmissionKind
    submission: new_item_dialog.ReviewSubmission
    content: bytes
    legacy_content: bytes = field(repr=False)

    @classmethod
    def plan(cls, source: Path, target: Path, owner: staff.StaffId, identity: str,
             kind: new_item_dialog.SubmissionKind, submission: new_item_dialog.ReviewSubmission) -> ReviewRecordMove:
        field = b"production_id" if kind == new_item_dialog.SubmissionKind.QUESTION else b"owner"
        value = owner.encode() if field == b"production_id" else b'"' + owner.encode() + b'"'
        content = source.read_bytes()
        header_start = content.index(b"\n") + 1
        fence = re.search(rb"(?m)^---\r?$", content[header_start:])
        if fence is None:
            raise TidyRefused(f"{source}: the record has no closing header fence")
        end = header_start + fence.start()
        header, count = re.subn(rb"(?m)^" + field + rb": [^\r\n]*", field + b": " + value, content[:end])
        if count != 1:
            raise TidyRefused(f"{source}: the record has no unique owner field")
        hashed_header = hash_move_header(header, kind, submission)
        return cls(source.resolve(), target.parent.resolve() / target.name, owner, identity, kind, submission,
                   hashed_header + content[end:], header + content[end:] if hashed_header != header else b"")

    def check_collision(self, occupied: set[Path]) -> None:
        if self.source == self.target or occupied - {self.target} or (os.path.lexists(self.target) and not (
                self.target.is_file() and not self.target.is_symlink()
                and self.target.read_bytes() in (self.content, self.legacy_content or self.content))):
            raise TidyRefused(f"{self.identity}: ID collision in {self.owner}'s store; kept {self.source}")

    @contextmanager
    def publish_record(self, numbers: record_store.LockedRecordNumbers) -> Generator[None]:
        exists = os.path.lexists(self.target)
        with numbers.write(self.target, self.identity, self.owner, new_record=not exists):
            if exists:
                self.check_collision(set())
                if self.legacy_content and self.target.read_bytes() == self.legacy_content:
                    mode = stat.S_IMODE(self.target.stat().st_mode)
                    atomic_file.write_bytes_atomic(self.target, self.content, mode=mode)
                yield
                return
            mode = stat.S_IMODE(self.source.stat().st_mode)
            with atomic_file.prepared_replacement(self.target, self.content, mode=mode) as temporary:
                try:
                    os.link(temporary, self.target)
                except FileExistsError as error:
                    raise TidyRefused(f"{self.identity}: ID collision at {self.target}; kept {self.source}") from error
                try:
                    atomic_file.sync_folder(self.target.parent)
                    atomic_file.sync_folder(self.target.parent.parent)
                    yield
                except Exception:
                    if self.target.exists() and self.target.samefile(temporary):
                        self.target.unlink()
                        atomic_file.sync_folder(self.target.parent)
                    raise

    def apply(self, numbers: record_store.LockedRecordNumbers) -> None:
        from scripts.review_links import review_submissions

        request_id = self.submission.request_id
        recipient = new_item_dialog.request_recipient(self.kind, request_id)
        address = review_submissions.SubmissionAddress(self.kind, recipient, request_id)
        hint = review_submissions._request_hint(address)  # pyright: ignore[reportPrivateUsage]
        if isinstance(hint, review_submissions.UnreadableRequestFile):
            raise hint.error
        original = self.source
        if isinstance(hint, review_submissions.NativeSubmissionDestination):
            if (hint.address != address or hint.id != self.identity
                    or hint.payload_hash != self.submission.request_digest):
                raise TidyRefused(f"{self.identity}: the Review receipt disagrees with {self.source}")
            original = hint.original_path
        elif (isinstance(hint, review_submissions.PendingFiling)
              and hint.payload_hash != self.submission.request_digest):
            raise TidyRefused(f"{self.identity}: the pending Review request disagrees with {self.source}")
        destination = review_submissions.NativeSubmissionDestination(
            self.identity, self.kind, recipient, request_id, self.submission.request_digest,
            self.target, original, self.owner)
        folder = review_submissions.image_folder(self.submission).parent
        receipt, pending = folder / "receipt.json", folder / "pending.json"
        with (self.publish_record(numbers),
              record_store.keep_file_on_error(receipt),
              record_store.keep_file_on_error(pending),
              record_store.keep_file_on_error(self.source)):
            review_submissions._write_location(receipt, destination)  # pyright: ignore[reportPrivateUsage]
            pending.unlink(missing_ok=True)
            atomic_file.sync_folder(folder)
            self.source.unlink()
            atomic_file.sync_folder(self.source.parent)


def keep_open_review_items(owner: staff.StaffRow, units: tuple[staff.StaffRow, ...], document: Path,
                           identities: QuestionStaff) -> None:
    """Move the open user filings before closing records or clearing state."""
    from scripts.production import update_registration as questions
    from scripts.review_links import review_submissions

    members = identities.record
    parent = next((row for identity in investigation.owner_chain(owner.id, members)[1:]
                   if (row := members.by_id(identity)) is not None and row.kind == "showrunner"
                   and review_submissions.owner_is_active(row)), None)
    if parent is None and not units:
        return
    target = owner if parent is None else parent
    sources = (*(() if parent is None else (owner,)), *units)
    assert isinstance(target.doc, (staff.LiveDoc, staff.WrappedDoc))
    with (investigation.recipient_locks(target.id, *(row.id for row in sources)),
          questions.wrap_question_locks(target.id, *(row.id for row in sources))):
        moves: list[ReviewRecordMove] = []
        for row in sources:
            for path, item in investigation.open_review_investigations(row.id):
                assert isinstance(item.submission, new_item_dialog.ReviewSubmission)
                moves.append(ReviewRecordMove.plan(path, investigation.folder(target.id) / f"{item.id}.md",
                                                   target.id, item.id, new_item_dialog.SubmissionKind(item.kind.value),
                                                   item.submission))
            records = (questions.question_records_for_document(document, identities, strict=True) if row.id == owner.id
                       else questions.question_records_for_owner(row.id, statuses=("open",), strict=True))
            for record in records:
                if not questions.pending_review_reply(record):
                    continue
                folder = questions.question_folder(target.doc.path, identities)
                if not isinstance(folder, questions.QuestionFolder):
                    raise TidyRefused(f"{target.name}: no Question store")
                details = new_item_dialog.read_submission(record.item.get("review_submission"))
                identity = record.item.get("review_id", "")
                if re.fullmatch(r"Q[1-9][0-9]*", identity) is None:
                    raise TidyRefused(f"{record.path}: the sent Question has no stored ID")
                moves.append(ReviewRecordMove.plan(record.path, questions.review_question_path(folder, details.request_id),
                                                   target.id, identity, new_item_dialog.SubmissionKind.QUESTION, details))
        if not moves:
            return
        apply_review_moves(moves, target.doc.path, identities)


def apply_review_moves(moves: list[ReviewRecordMove], parent_doc: Path, identities: QuestionStaff) -> None:
    """Check all IDs before changing a native record."""
    from scripts.production import update_registration as questions

    owner = moves[0].owner
    with investigation.investigation_numbers() as investigation_numbers, questions.question_numbers() as question_numbers:
        occupied: dict[tuple[record_store.RecordKind, str], set[Path]] = {}
        if any(move.kind == new_item_dialog.SubmissionKind.QUESTION for move in moves):
            for record in questions.question_records_for_document(parent_doc, identities, strict=True):
                _ = occupied.setdefault((record_store.RecordKind.QUESTION, record.item.get("review_id", "")), set()).add(record.path)
        if any(move.kind != new_item_dialog.SubmissionKind.QUESTION for move in moves):
            for path, identity in investigation.review_record_ids(owner, strict=True):
                _ = occupied.setdefault((record_store.RecordKind.INVESTIGATION, identity), set()).add(path)
        for move in moves:
            kind = (record_store.RecordKind.QUESTION if move.kind == new_item_dialog.SubmissionKind.QUESTION
                    else record_store.RecordKind.INVESTIGATION)
            paths = occupied.setdefault((kind, move.identity), set())
            move.check_collision(paths)
            paths.add(move.source)
        for move in moves:
            move.apply(question_numbers if move.kind == new_item_dialog.SubmissionKind.QUESTION else investigation_numbers)


def tidy(production_name: str, document: Path | None = None, *, identities: QuestionStaff | None = None) -> list[Path]:
    """Close the production's questions and delete its state files."""
    from scripts.production.update_registration import QuestionFolder, QuestionStaff, RegistrationFailure, close_questions, question_folder

    identities = identities if identities is not None else QuestionStaff.read()
    row = identities.owner(document) if document is not None else showrunners.owner_for(production_name, identities.record)
    paths = owned_paths(row)
    units = () if document is None else tuple(identities.record.find(kind="unit", parent=row.id))
    if document is not None:
        try:
            keep_open_review_items(row, units, document, identities)
        except (investigation.InvestigationError, RegistrationFailure, new_item_dialog.SubmissionError) as error:
            raise TidyRefused(str(error)) from error
    if document is not None and can_hold_questions(document):
        try:
            folder = question_folder(document, identities)
            if isinstance(folder, QuestionFolder):
                close_questions(document, identities)
                directory = paths[-1]
                if folder.path.parent == directory and directory.is_dir():
                    paths = (*paths[:-1], *(path for path in directory.iterdir() if path != folder.path))
        except staff.StaffRefused as error:
            raise TidyRefused(str(error)) from error
    owners = {str(member.id) for member in (row, *units)}
    try:
        for owner in sorted(owners):
            copies.remove_trees(owner)
    except copies.CopyError as error:
        raise TidyRefused(str(error)) from error
    roots = {root for kind in record_store.RecordKind
             for root in record_store.staff_store_folders(kind, members=identities.record)}
    protected = roots.union(record_store.archive_folders(roots))
    removed: list[Path] = []

    def clean(path: Path) -> None:
        location = path.resolve()
        if any(location.is_relative_to(root) for root in protected):
            return
        if any(root.is_relative_to(location) and root.exists() for root in protected):
            if path.is_dir() and not path.is_symlink():
                for child in path.iterdir():
                    clean(child)
            return
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.exists() or path.is_symlink():
            path.unlink()
        else:
            return
        removed.append(path)

    for path in paths:
        clean(path)
    return removed


def wrapped_owner(doc: Path, record: staff.StaffSnapshot) -> staff.StaffRow:
    """Read the wrapped owner's staff name and require no update timer."""
    owner = unit_lookup.doc_owner(doc, record)
    lines = doc.read_text(encoding="utf-8").splitlines()
    status = next((line for line in lines if line.startswith("> **Status:")), "")
    if not status.startswith(WRAPPED):
        raise TidyRefused(f"{doc}: not wrapped")
    if (NOTIFIER_STATE_DIR / f"showrunner-{owner.id}").exists():
        raise TidyRefused(f"{doc}: {owner.name} still has an update timer")
    return owner


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: tidy_wrapped.py <production doc>...", file=sys.stderr)
        return 2
    from scripts.production.update_registration import QuestionStaff, RegistrationFailure

    identities: QuestionStaff | None = None
    failed = False
    for name in argv:
        try:
            if identities is None:
                identities = QuestionStaff.read()
            owner = wrapped_owner(Path(name), identities.record)
            removed = tidy(owner.name, Path(name), identities=identities)
        except (TidyRefused, RegistrationFailure, staff.StaffRefused, staff.StaffUnreadable,
                OSError, UnicodeError, ValueError) as error:
            print(f"kept: {error}", file=sys.stderr)
            failed = True
            continue
        count = len(removed)
        print(f"{owner.name}: removed {count} state file{'' if count == 1 else 's'}" if count
              else f"{owner.name}: no state files")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

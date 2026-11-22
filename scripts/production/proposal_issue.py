#!/usr/bin/env python3
"""Edit proposal fields under an issue lock.

Store staff ids in unit, proposed_by and approved_in.
Read current names from staff. Keep proposal numbers unique across issues and archives.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from collections.abc import Callable, Collection, Generator, Iterable, Iterator, Mapping
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from functools import cached_property
from itertools import chain
from pathlib import Path
from typing import cast


from scripts.lib import atomic_file, urgency  # noqa: E402
from scripts.lib.user_zone import user_zone  # noqa: E402
from scripts.production.issues import ALL_FIELDS, PROPOSAL_TAG, Issue, frontmatter, header_fields
from scripts.production import record_store, staff, unit_names
from scripts.review_links import new_item_dialog, reply_tokens, review_events
from scripts.review_links.review_events import MarkChange, NoMark, ReviewLog, ReviewMark, StoredReviewLog

FENCE = "---"
# Values the vault writes bare; every other value is written double-quoted.
BARE = frozenset({"stage", "status", "review_submission"})
KEY = re.compile(r"^([A-Za-z_][\w-]*):")
GOAL = re.compile(r"^\d+ - \S.*$")
STAR = "⭐"
# The fields a migrated decision can take from the old issues it merges.
INHERITED_FIELDS = frozenset({"unit", "approved_by", "approved_in", "stage", "backlog_goal", "backlog_alignment",
                              "backlog_impact", "backlog_urgency", "backlog_effort"})
# The fields close adds or changes; reopen removes them and sets status open.
CLOSE_FIELDS = ("as_built", "reason", "date_closed")
PROPOSED_RANKING = re.compile(r"^Proposed ranking:[^\r\n]*", re.MULTILINE)
EMPTY_RANKING = re.compile(r"^Proposed ranking:[ \t]*(?=\r?$)", re.MULTILINE)
NUMBER_FIELD = "proposal_id"
NUMBER = re.compile(r'^proposal_id:\s*"?P(\d+)"?\s*$', re.MULTILINE)
# The vault keeps closed issues under archive/ beside the issues folder; their numbers stay taken.
ARCHIVE = "archive"
STYLE_AREA = "style"


class Refusal(ValueError):
    """The issue file cannot be edited."""


def _quoted(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _value_line(key: str, value: str) -> str:
    return f"{key}: {value if key in BARE else _quoted(value)}\n"


def _closing_fence(lines: list[str], path: Path) -> int:
    if not lines or lines[0].rstrip("\r\n") != FENCE:
        raise Refusal(f"{path} has no frontmatter")
    for index in range(1, len(lines)):
        if lines[index].rstrip("\r\n") == FENCE:
            return index
    raise Refusal(f"{path} has no closing --- for its frontmatter")


def _key_span(lines: list[str], end: int, key: str) -> tuple[int, int] | None:
    """The key's line and its continuation lines, as a slice, or None when the key is absent."""
    for start in range(1, end):
        match = KEY.match(lines[start])
        if match is None or match.group(1) != key:
            continue
        stop = start + 1
        while stop < end and KEY.match(lines[stop]) is None:
            stop += 1
        return start, stop
    return None


def set_fields(text: str, fields: dict[str, str], path: Path) -> tuple[str, list[str]]:
    """The text with each field set, and the keys whose lines changed."""
    lines = record_store.lines_with_endings(text)
    changed: list[str] = []
    for key, value in fields.items():
        end = _closing_fence(lines, path)
        line = _value_line(key, value)
        span = _key_span(lines, end, key)
        if span is None:
            lines.insert(end, line)
            changed.append(key)
            continue
        start, stop = span
        newline = lines[start][len(lines[start].rstrip("\r\n")):] or "\n"
        replacement = line[:-1] + newline
        if lines[start:stop] != [replacement]:
            lines[start:stop] = [replacement]
            changed.append(key)
    return "".join(lines), changed


def remove_fields(text: str, keys: tuple[str, ...], path: Path) -> tuple[str, list[str]]:
    """The text without each key and its continuation lines, and the keys it removed."""
    lines = record_store.lines_with_endings(text)
    removed: list[str] = []
    for key in keys:
        span = _key_span(lines, _closing_fence(lines, path), key)
        if span is not None:
            del lines[span[0]:span[1]]
            removed.append(key)
    return "".join(lines), removed


def set_list_fields(text: str, fields: dict[str, list[str]], path: Path) -> tuple[str, list[str]]:
    """Set JSON lists without changing other frontmatter or body bytes."""
    lines = record_store.lines_with_endings(text)
    changed: list[str] = []
    for key, values in fields.items():
        end = _closing_fence(lines, path)
        span = _key_span(lines, end, key)
        start, stop = span if span is not None else (end, end)
        newline = lines[start][len(lines[start].rstrip("\r\n")):] or "\n"
        replacement = f"{key}: {json.dumps(values, ensure_ascii=False)}{newline}"
        if lines[start:stop] != [replacement]:
            lines[start:stop] = [replacement]
            changed.append(key)
    return "".join(lines), changed


REVIEW_LOG_FIELD = "review_log"
REVIEW_MARK_FIELD = "review_state"


def review_log(issue: Issue) -> tuple[ReviewLog, ...]:
    """The Review page log: one JSON object for each item of the block list."""
    return review_events.from_rows([cast(StoredReviewLog, json.loads(item)) for item in issue.fields.get(REVIEW_LOG_FIELD, [])])


def review_mark(issue: Issue) -> ReviewMark | NoMark:
    value = issue.value(REVIEW_MARK_FIELD)
    return ReviewMark(value) if value else NoMark()


def pending_review_reply(issue: Issue) -> bool:
    return not review_is_done(issue) and review_mark(issue) == ReviewMark.WAITING


@dataclass(frozen=True)
class ProposalSubmissionReply:
    owner: staff.StaffId
    key: new_item_dialog.ScopedSubmissionKey
    token: str


@dataclass(frozen=True)
class NoProposalSubmissionReply:
    pass


_NO_SUBMISSION_REPLY = NoProposalSubmissionReply()

def record_review(path: Path, words: str | tuple[str, ...], change: MarkChange, today: date, who: str = "nate", *,
                  check: Callable[[], object] | None = None, waiting_why: str = "", check_state: Callable[[], None] = lambda: None,
                  new_reply_proof: reply_tokens.ReplyTokenProof | reply_tokens.MintReplyProof = reply_tokens.MINT_REPLY_PROOF,
                  submission_reply: ProposalSubmissionReply | NoProposalSubmissionReply = _NO_SUBMISSION_REPLY) -> bool:
    """Append one Review page log line and change its mark. Returns False for an exact repeat."""
    reason = review_events.require_waiting_why(waiting_why) if change == ReviewMark.WAITING or waiting_why else ""
    with issue_lock(path):
        if check is not None:
            _ = check()
        issue = Issue(path, frontmatter(path))
        check_state()
        before = review_log(issue)
        if isinstance(submission_reply, ProposalSubmissionReply):
            origin = review_submission(issue)
            key = submission_reply.key
            if (proposal_owner(issue, issue.value("proposed_by")) != submission_reply.owner or issue.value(NUMBER_FIELD) != key.item_id
                    or not isinstance(origin, new_item_dialog.ReviewSubmission) or origin.request_id != key.request_id):
                raise new_item_dialog.SubmissionError("The selected Proposal changed.")
            new_item_dialog.require_reply_token(origin, submission_reply.token)
            latest = next((entry.words for entry in reversed(before)
                           if entry.words.startswith(("Answer: ", "question: "))), "")
            closed = review_is_done(issue)
            repeated = not closed and isinstance(review_mark(issue), NoMark) and latest == words
            if not repeated and not pending_review_reply(issue):
                raise new_item_dialog.SubmissionError("This item no longer accepts this reply.")
            if repeated:
                return False
        lines_to_append = (words,) if isinstance(words, str) else words
        if not isinstance(submission_reply, ProposalSubmissionReply) and issue.value("review_submission") and any(
                line.startswith("Answer: ") for line in lines_to_append):
            raise new_item_dialog.SubmissionError("Use the scoped reply from the Review item.")
        events = before
        for line in lines_to_append:
            events = review_events.append(events, ReviewLog(datetime.now(timezone.utc), who, line))
        mark = review_events.changed_mark(review_mark(issue), change)
        new_wait: dict[str, str] = {}
        if change == ReviewMark.WAITING and issue.value("review_submission"):
            fields = cast(dict[str, object], json.loads(issue.value("review_submission")))
            _ = new_item_dialog.read_submission(fields)
            proof = new_reply_proof if isinstance(new_reply_proof, reply_tokens.ReplyTokenProof) else reply_tokens.mint()
            new_wait["review_submission"] = json.dumps(new_item_dialog.renewed_submission_fields(fields, proof))
        if not new_wait and events == before and mark == review_mark(issue) and (not reason or reason == issue.value("waiting_why")):
            return False
        lines = record_store.lines_with_endings(read_issue(path))
        end = _closing_fence(lines, path)
        start, stop = _key_span(lines, end, REVIEW_LOG_FIELD) or (end, end)
        newline = lines[start][len(lines[start].rstrip("\r\n")):] or "\n"
        lines[start:stop] = [f"{REVIEW_LOG_FIELD}:{newline}",
                             *(f"  - {json.dumps(row, ensure_ascii=False)}{newline}" for row in review_events.rows(events))]
        text, _ = remove_fields("".join(lines), (REVIEW_MARK_FIELD,), path)
        marked = {REVIEW_MARK_FIELD: mark.value} if isinstance(mark, ReviewMark) else {}
        why = {"waiting_why": reason} if reason else {}
        text, _ = set_fields(text, {**marked, **why, **new_wait, "date_modified": _vault_date(today)}, path)
        write_issue(path, text)
        return True


@contextmanager
def issue_lock(path: Path) -> Generator[None]:
    """Hold an exclusive lock on the issue. The lock file is named from the issue's resolved path."""
    folder = Path.home() / ".local/state/proposal-issue/locks"
    folder.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:32]
    with (folder / f"proposal-issue-{digest}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


@contextmanager
def _folder_lock(folder: Path) -> Generator[None]:
    """Hold an exclusive lock on the issues folder, so two filings never take one number."""
    with issue_lock(folder / ".proposal-numbers"):
        yield


def _proposal_id(path: Path) -> str:
    return record_store.number_fields(path, frozenset({NUMBER_FIELD})).value(NUMBER_FIELD)


def proposal_numbers() -> AbstractContextManager[record_store.LockedRecordNumbers]:
    return record_store.record_numbers(record_store.RecordKind.PROPOSAL, _proposal_id)


def proposal_owner(issue: Issue, legacy_owner: str) -> staff.StaffId:
    return record_store.record_owner(record_store.RecordKind.PROPOSAL, issue, legacy_owner)


def _proposer_production(proposer: str, members: staff.StaffSnapshot) -> staff.StaffId:
    row = members.by_member_reference(proposer) or members.by_path(proposer)
    if row is not None and row.kind == "unit":
        row = members.parent_of(row)
    return row.id if row is not None and row.kind in ("showrunner", "producer") else staff.StaffId("")


@dataclass(frozen=True)
class DecisionStores:
    """The production folders used to find legacy decisions."""

    owner: staff.StaffId
    members: staff.StaffSnapshot
    strict: bool

    @cached_property
    def production_folders(self) -> dict[staff.StaffId, tuple[Path, ...]]:
        kind = record_store.RecordKind.PROPOSAL
        return {owner: record_store.known_folders(kind, owner, document=record_store.local_document(row),
                                                  strict=self.strict, require_existing_document=True)
                for owner, row in record_store.store_owners(kind, self.members, strict=self.strict).items()}

    def owns(self, issue: Issue, *, local: bool) -> bool:
        if owner := issue.value("production_id"):
            return local and owner == self.owner
        proposer = issue.value("proposed_by")
        if not proposer:
            return local
        row = self.members.by_member_reference(proposer) or self.members.by_path(proposer)
        if row is None or row.kind != "unit":
            return local and (row.id if row is not None else proposer) == self.owner
        parent = self.members.parent_of(row)
        current = parent is not None and parent.id == self.owner
        if local and current:
            return True
        if not all(issue.value(key) for key in ("style_rule", "style_crate", "style_rule_blob")):
            return False
        if not local:
            return current
        # A store folder can identify the unit's former production.
        owners = {owner for owner, roots in self.production_folders.items()
                  if issue.path.parent in roots or any(issue.path.is_relative_to(root)
                                                      for root in record_store.archive_folders(roots))}
        return owners == {self.owner}

    def former_paths(self, roots: tuple[Path, ...], *, strict: bool) -> Iterator[Path]:
        if self.members.find(kind="unit", parent=self.owner):
            others = (root for owner, folders in self.production_folders.items() if owner != self.owner
                      for root in folders if root not in roots)
            yield from record_store.stored_paths(others, strict=strict)


def _proposal_owner(folder: Path, proposer: str, members: staff.StaffSnapshot | None = None,
                    *, strict: bool = False) -> staff.StaffId:
    """Resolve the proposer or use the folder history for a legacy record."""
    members = staff.snapshot() if members is None else members
    if proposer and (owner := _proposer_production(proposer, members)):
        return owner
    folder = folder.resolve()
    for include_history in (False, True):
        for owner, member in record_store.store_owners(record_store.RecordKind.PROPOSAL, members, strict=strict).items():
            if member is not None and member.kind not in ("showrunner", "producer"):
                continue
            document = record_store.local_document(member)
            roots = record_store.known_folders(record_store.RecordKind.PROPOSAL, owner, document=document,
                                               include_history=include_history, strict=strict,
                                               require_existing_document=strict)
            if folder in roots or any(folder.is_relative_to(root) for root in record_store.archive_folders(roots)):
                return owner
    return staff.StaffId("")


def _proposal_folders(folder: Path, owner: staff.StaffId, *, strict: bool = False,
                      members: staff.StaffSnapshot | None = None,
                      require_existing_document: bool = False) -> tuple[Path, ...]:
    row = (staff.snapshot() if members is None else members).by_id(owner) if owner else None
    document = record_store.local_document(row)
    return record_store.known_folders(record_store.RecordKind.PROPOSAL, owner, (folder,), document=document,
                                      strict=strict, require_existing_document=require_existing_document)


def proposal_store_issues(folder: Path, *, owner: staff.StaffId | None = None) -> Iterator[Issue]:
    """Read issue headers from this Proposal store and its former folders."""
    members = staff.snapshot()
    roots = _proposal_folders(folder, _proposal_owner(folder, "", members) if owner is None else owner, members=members)
    paths = sorted(record_store.stored_paths(roots))
    if owner is None:
        for path in paths:
            if fields := record_store.read_header(path):
                yield Issue(path, fields)
        return
    stores = DecisionStores(owner, members, strict=False)
    local = set(paths)
    paths.extend(sorted(set(stores.former_paths(roots, strict=False)) - local))
    for path in paths:
        if fields := record_store.read_header(path):
            issue = Issue(path, fields)
            if stores.owns(issue, local=path in local):
                yield issue


def _free_proposal_path(path: Path, identity: str, names: Collection[str]) -> Path:
    if path.name in names or path.exists() or path.is_symlink():
        path = path.with_stem(f"{path.stem} {identity.lower()}")
    return record_store.free_record_path(path, names)


@dataclass(frozen=True)
class ProposalNumbering:
    path: Path
    owner: staff.StaffId
    identity: str
    text: str


def number(paths: list[Path], today: date) -> list[str]:
    """Check all proposals before writing their numbers, oldest first."""
    paths = sorted({path.resolve() for path in paths})
    with ExitStack() as locks:
        for folder in sorted({path.parent.resolve() for path in paths}):
            _ = locks.enter_context(_folder_lock(folder))
        for path in paths:
            _ = locks.enter_context(issue_lock(path))
        numbers = locks.enter_context(proposal_numbers())
        issues = sorted((record_store.number_fields(path, ALL_FIELDS) for path in paths),
                        key=lambda issue: (issue.value("date_created"), issue.path.name))
        writes: list[ProposalNumbering] = []
        lines: list[str] = []
        folders = sorted({path.parent for path in paths})
        ceiling = numbers.floor(*folders)
        for issue in issues:
            if PROPOSAL_TAG not in issue.fields.get("tags", []):
                lines.append(f"number {issue.path.name}: not a proposal")
                continue
            text = read_issue(issue.path)
            taken = NUMBER.search(text.split(f"\n{FENCE}", 1)[0])
            if taken:
                lines.append(f"number {issue.path.name}: P{taken[1]}")
                continue
            folder = issue.path.parent.resolve()
            content = record_store.lines_with_endings(text)
            header = Issue(issue.path, header_fields(content[1:_closing_fence(content, issue.path)]))
            owner = proposal_owner(header, "") or _proposal_owner(folder, header.value("proposed_by"))
            ceiling += 1
            identity = f"P{ceiling}"
            numbered, _ = set_fields(text, {NUMBER_FIELD: identity, "production_id": owner, "date_modified": _vault_date(today)}, issue.path)
            writes.append(ProposalNumbering(issue.path, owner, identity, numbered))
            lines.append(f"number {issue.path.name}: {identity}")
        with ExitStack() as rollback:
            for write in writes:
                _ = rollback.enter_context(record_store.keep_file_on_error(write.path))
                with numbers.write(write.path, write.identity, write.owner):
                    _replace_issue(write.path, write.text)
        return lines


def review_submission(issue: Issue) -> new_item_dialog.SubmissionOrigin:
    return new_item_dialog.decode_submission(issue.value("review_submission"))


def find_review_proposal(folders: Path | tuple[Path, ...], recipient: staff.StaffId,
                         lookup: new_item_dialog.ReviewSubmissionLookup, *, document: Path | None = None) -> Issue | None:
    """Find a sent Proposal in the specified folders and the owner's folder history."""
    keys = frozenset((NUMBER_FIELD, "production_id", "proposed_by", "review_submission", "tags"))
    folders = (folders,) if isinstance(folders, Path) else folders
    roots = record_store.known_folders(record_store.RecordKind.PROPOSAL, recipient, folders, document=document,
                                       strict=True, require_document=False)
    for path in record_store.stored_paths(roots, strict=True):
        issue = Issue(path, record_store.read_header(path, keys=keys, strict=True))
        if proposal_owner(issue, issue.value("proposed_by")) != recipient or not issue.value("review_submission"):
            continue
        if isinstance(lookup, new_item_dialog.ByItemId):
            if issue.value(NUMBER_FIELD) != lookup.value:
                continue
        details = review_submission(issue)
        if not isinstance(details, new_item_dialog.ReviewSubmission):
            continue
        if isinstance(lookup, new_item_dialog.ByRequestId) and details.request_id != lookup.value:
            continue
        if (PROPOSAL_TAG not in issue.fields.get("tags", ())
                or not re.fullmatch(r"P[1-9][0-9]*", issue.value(NUMBER_FIELD))):
            raise OSError(f"Cannot read a saved proposal at {path}.")
        return issue
    return None


def file_review_proposal(folder: Path, recipient: staff.StaffId, title: str, words: str, waiting_why: str,
                         details: new_item_dialog.ReviewSubmission, *,
                         before_write: Callable[[str, Path], None] = lambda _id, _path: None) -> Issue:
    """File the user's proposal with a P number in the existing vault."""
    slug = " ".join(re.sub(r"[^\w ]", " ", title.lower()).split())[:50].rstrip() or "proposal"
    today = datetime.now(user_zone()).date()
    why = review_events.require_waiting_why(waiting_why)
    with _folder_lock(folder), proposal_numbers() as numbers:
        folders = _proposal_folders(folder, recipient, strict=True)
        names = {path.name for path in record_store.stored_paths(folders, strict=True)}
        identity = numbers.next_id(*folders, covered_owner=recipient)
        # An inline JSON object stays one frontmatter value in the issue reader.
        details_text = json.dumps(new_item_dialog.stored_submission(details))
        draft = f"---\ntags: [issue, proposal]\nreview_submission: {details_text}\n---\n\n{words}\n"
        fields = {"status": "open", "stage": "backlog", "record_type": "proposal",
                  "proposed_by": recipient, "production_id": recipient,
                  "date_created": _vault_date(today), "date_modified": _vault_date(today),
                  REVIEW_MARK_FIELD: ReviewMark.WAITING, "waiting_why": why}
        while True:
            path = _free_proposal_path(folder / f"{slug}.md", identity, names)
            text, _ = set_fields(draft, {**fields, NUMBER_FIELD: identity}, path)
            before_write(identity, path)
            try:
                with numbers.write(path, identity, recipient, new_record=True):
                    try:
                        record_store.create_record_text(path, text)
                    except record_store.ReusedReviewRecord:
                        found = Issue(path, record_store.read_header(path, strict=True))
                        saved_id = found.value(NUMBER_FIELD)
                        if (proposal_owner(found, found.value("proposed_by")) != recipient
                                or PROPOSAL_TAG not in found.fields.get("tags", []) or not numbers.number(saved_id)):
                            raise OSError(f"Cannot read a saved proposal at {path}.") from None
                        before_write(saved_id, path)
                        return found
            except record_store.OccupiedRecordPath:
                names.add(path.name)
                identity = numbers.next_id_after_collision(identity, path)
                continue
            lines = text.splitlines()
            return Issue(path, header_fields(lines[1:_closing_fence(lines, path)]))


def reply_to_submission(path: Path, key: new_item_dialog.ScopedSubmissionKey, words: str, token: str, *,
                        owner: staff.StaffId) -> bool:
    return record_review(path, "Answer: " + words, NoMark(), datetime.now(user_zone()).date(), owner,
                         submission_reply=ProposalSubmissionReply(owner, key, token))


def edit_rules(path: Path, urls: list[str], today: date) -> list[str]:
    with issue_lock(path):
        text, changed = set_list_fields(read_issue(path), {"style_rules": sorted(set(urls))}, path)
        text, dates = set_fields(text, {"date_modified": _vault_date(today)}, path)
        changed.extend(dates)
        if changed:
            write_issue(path, text)
        return changed


def read_issue(path: Path) -> str:
    if not path.is_file():
        raise Refusal(f"{path} is not a file")
    with path.open(encoding="utf-8", newline="") as source:
        return source.read()


def write_issue(path: Path, text: str) -> None:
    lines = record_store.lines_with_endings(text)
    end = _closing_fence(lines, path)
    fields = Issue(path, header_fields(lines[1:end]))
    identity = fields.value(NUMBER_FIELD)
    origin = review_submission(fields)
    if isinstance(origin, new_item_dialog.ReviewSubmission):
        value = cast(dict[str, object], json.loads(fields.value("review_submission")))
        updated = new_item_dialog.hashed_submission_fields(value, origin)
        if updated is not value:
            span = _key_span(lines, end, "review_submission")
            assert span is not None
            start, _ = span
            newline = lines[start][len(lines[start].rstrip("\r\n")):]
            lines[start] = "review_submission: " + json.dumps(updated) + newline
            text = "".join(lines)
    if not re.fullmatch(r"P[1-9][0-9]*", identity):
        _replace_issue(path, text)
        return
    owner = proposal_owner(fields, fields.value("proposed_by") if fields.value("review_submission") else "")
    if not owner:
        owner = _proposal_owner(path.parent, fields.value("proposed_by"))
    with proposal_numbers() as numbers, numbers.write(path, identity, owner):
        _replace_issue(path, text)


def renew_reply_proof(issue: Issue, proof: reply_tokens.ReplyTokenProof) -> None:
    """Replace the proof under the Proposal lock."""
    fields = cast(dict[str, object], json.loads(issue.value("review_submission")))
    updated = new_item_dialog.renewed_submission_fields(fields, proof)
    text, _ = set_fields(read_issue(issue.path), {"review_submission": json.dumps(updated)}, issue.path)
    write_issue(issue.path, text)


def scrub_reply_token(path: Path) -> bool:
    with issue_lock(path):
        text = read_issue(path)
        lines = record_store.lines_with_endings(text)
        fields = Issue(path, header_fields(lines[1:_closing_fence(lines, path)]))
        raw = fields.value("review_submission")
        if not raw:
            return False
        value = cast(dict[str, object], json.loads(raw))
        if "reply_token" not in value:
            return False
        write_issue(path, text)
        return True


def _replace_issue(path: Path, text: str) -> None:
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as temporary:
            temporary_name = temporary.name
            _ = temporary.write(text)
        os.replace(temporary_name, path)
        temporary_name = ""
    finally:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)


def _stars(name: str, value: str) -> str:
    """One to five stars, from a count or from the stars themselves."""
    count = int(value) if value.isdigit() else len(value) if value and value == STAR * len(value) else 0
    if not 1 <= count <= 5:
        raise Refusal(f"--{name} takes 1 to 5 (or one to five {STAR}), not {value!r}")
    return STAR * count


def _vault_date(today: date) -> str:
    return f"[[{today.isoformat()}]]"


def _clear_waiting_mark(text: str, path: Path) -> tuple[str, list[str]]:
    lines = text.split("\n")
    fields = header_fields(lines[1:_closing_fence(lines, path)], frozenset({REVIEW_MARK_FIELD}))
    if review_mark(Issue(path, fields)) != ReviewMark.WAITING:
        return text, []
    return remove_fields(text, (REVIEW_MARK_FIELD,), path)


def edit(path: Path, fields: dict[str, str], today: date, *, check: Callable[[], object] | None = None) -> list[str]:
    """Set the fields and date_modified in the issue at path. Returns the keys whose lines changed."""
    with issue_lock(path):
        if check is not None:
            _ = check()
        text = read_issue(path)
        text, removed = _clear_waiting_mark(text, path) if fields.get("stage") == IN_PROGRESS_STAGE else (text, [])
        text, changed = set_fields(text, {**fields, "date_modified": _vault_date(today)}, path)
        if removed or changed:
            write_issue(path, text)
        return [*removed, *changed]


def reopen(path: Path, today: date) -> list[str]:
    """Remove the close fields and set status open. The answer stays, and an approved one is in progress."""
    with issue_lock(path):
        reopened = {"status": "open"}
        if isinstance(decision_answer(Issue(path, frontmatter(path))), ApprovedDecision):
            reopened["stage"] = IN_PROGRESS_STAGE
        text, removed = remove_fields(read_issue(path), CLOSE_FIELDS, path)
        text, changed = set_fields(text, reopened, path)
        if not removed and not changed:
            return []
        text, dates = set_fields(text, {"date_modified": _vault_date(today)}, path)
        write_issue(path, text)
        return [*removed, *changed, *dates]


USER = "user"
SHOWRUNNER = "showrunner"
APPROVERS = (USER, SHOWRUNNER)
# The fields approve sets; unapprove writes them empty, as filing does.
APPROVAL_FIELDS = ("unit", "approved_by", "approved_in")
REMOVED_FIELDS = ("showrunner", "plan")


def staff_reference(reference: str, kind: staff.Kind) -> staff.StaffRow:
    """Resolve an exact staff id or an unambiguous staff name."""
    record = staff.snapshot()
    row = record.by_id(staff.StaffId(reference)) or record.by_path(reference)
    if row is None:
        try:
            name = unit_names.name(reference) if kind == "unit" else reference
        except unit_names.BadName:
            raise Refusal(f"{reference}: no unique {kind} staff row") from None
        matches = [member for member in record.rows if member.kind == kind and member.name == name]
        row = matches[0] if len(matches) == 1 else None
    if row is None or row.kind != kind:
        raise Refusal(f"{reference}: no unique {kind} staff row")
    return row


def staff_name(identity: str, record: staff.StaffSnapshot | None = None) -> str:
    """Read the current name for a stored staff id; a caller with many ids gives one snapshot."""
    row = (staff.snapshot() if record is None else record).by_id(staff.StaffId(identity))
    return row.name if row is not None else f"unavailable staff {identity}"


def proposal_unit(issue: Issue) -> str:
    """Read the current unit name from the proposal's stored staff id."""
    identity = issue.value("unit")
    row = staff.snapshot().by_id(staff.StaffId(identity))
    return row.name if row is not None else ""


def calling_showrunner() -> str:
    """Read the calling showrunner's fixed staff id."""
    record = staff.snapshot()
    session = staff.caller_identity()
    row = record.by_current_session(session)
    if row is None or row.kind != "showrunner":
        raise Refusal("this session is no production's showrunner; approve and unapprove run in one")
    return row.id


def approver(issue: Issue) -> str:
    """Who approved: showrunner only when approved_by says so; an approval without the field is the user's."""
    return SHOWRUNNER if issue.value("approved_by") == SHOWRUNNER else USER


def approve(path: Path, fields: dict[str, str], by: str, approved_in: str, today: date) -> list[str]:
    """Record the approval and stage active. A showrunner approval keeps the approver of a user one."""
    with issue_lock(path):
        return _approve(path, fields, by, approved_in, today)


def approve_unanswered(path: Path, fields: dict[str, str], by: str, approved_in: str, today: date) -> list[str]:
    """Approve as approve does, only while no answer is recorded, read under the same lock as the write."""
    with issue_lock(path):
        if not isinstance(decision_answer(Issue(path, frontmatter(path))), UnansweredDecision):
            return []
        return _approve(path, fields, by, approved_in, today)


def _approve(path: Path, fields: dict[str, str], by: str, approved_in: str, today: date) -> list[str]:
    """The approval write; the caller holds the issue lock."""
    issue = Issue(path, frontmatter(path))
    kept = by == SHOWRUNNER and isinstance(decision_answer(issue), ApprovedDecision) and approver(issue) == USER
    actor = {} if kept else {"approved_by": by, "approved_in": staff_reference(approved_in, "showrunner").id}
    text, removed = remove_fields(read_issue(path), REMOVED_FIELDS, path)
    text, unmarked = _clear_waiting_mark(text, path)
    removed.extend(unmarked)
    text, changed = set_fields(text, {"unit": staff_reference(fields["unit"], "unit").id, **actor, "stage": IN_PROGRESS_STAGE,
                                             "date_modified": _vault_date(today)}, path)
    if removed or changed:
        write_issue(path, text)
    return [*removed, *changed]


def unapprove(path: Path, approved_in: str, today: date, *, dry_run: bool) -> list[str]:
    """Set a proposal a showrunner approved in `approved_in` back to waiting for approval.

    A dry run refuses the same issues and writes nothing.
    """
    with issue_lock(path):
        issue = Issue(path, frontmatter(path))
        if issue.value("status") != "open" or PROPOSAL_TAG not in issue.fields.get("tags", []):
            raise Refusal(f"{path}: not an open proposal")
        if not isinstance(decision_answer(issue), ApprovedDecision):
            raise Refusal(f"{path}: no approval is recorded")
        if approver(issue) == USER:
            raise Refusal(f"{path}: the user approved it; only the user's decline undoes it")
        if issue.value("approved_in") != approved_in:
            raise Refusal(f"{path}: approved in {issue.value('approved_in') or 'no showrunner'}, not in {approved_in}")
        text, changed = set_fields(read_issue(path), {**dict.fromkeys(APPROVAL_FIELDS, ""), "stage": "backlog",
                                                 "date_modified": _vault_date(today)}, path)
        if not dry_run:
            write_issue(path, text)
        return changed


REVIEW_FIELDS = ("review_urgency", "review_due", "review_marked")


def set_urgency(path: Path, level: urgency.Urgency, today: date) -> list[str]:
    """Store the proposal's level for the user's review; normal stores nothing."""
    with issue_lock(path):
        original = read_issue(path)
        text, removed = remove_fields(original, REVIEW_FIELDS, path)
        text, changed = set_fields(text, urgency.fields(level, "review_"), path)
        if text == original:
            return []
        text, dates = set_fields(text, {"date_modified": _vault_date(today)}, path)
        write_issue(path, text)
        return [*dict.fromkeys([*removed, *changed]), *dates]


def fields_for(args: argparse.Namespace, today: date) -> dict[str, str]:
    verb = cast(str, args.verb)
    if verb == "start":
        return {"stage": IN_PROGRESS_STAGE}
    if verb == "close":
        detail = {"as_built": cast(str, args.as_built)} if cast(str | None, args.as_built) else {"reason": cast(str, args.reason)}
        return {**detail, "status": "closed", "date_closed": _vault_date(today)}
    goal = cast(str, args.goal)
    if GOAL.match(goal) is None:
        raise Refusal(f"--goal takes a goal like '1 - Ship Hana', not {goal!r}")
    return {
        "backlog_goal": goal,
        **{f"backlog_{name}": _stars(name, cast(str, getattr(args, name))) for name in (
            "alignment", "impact", "urgency", "effort",
        )},
    }


GENERATED_START = "<!-- style-decision:start -->"
GENERATED_END = "<!-- style-decision:end -->"


def decision_body(body: str) -> str:
    return GENERATED_START + "\n" + body.rstrip() + "\n" + GENERATED_END + "\n"


@dataclass(frozen=True)
class UnansweredDecision:
    """The user has not answered this decision."""


@dataclass(frozen=True)
class ApprovedDecision:
    unit: str


@dataclass(frozen=True)
class DeclinedDecision:
    words: str


def decision_answer(issue: Issue) -> UnansweredDecision | ApprovedDecision | DeclinedDecision:
    if issue.value("style_answer") == "declined":
        return DeclinedDecision(issue.value("style_answer_words"))
    if issue.value("unit"):
        return ApprovedDecision(issue.value("unit"))
    return UnansweredDecision()


IN_PROGRESS_STAGE = "active"


@dataclass(frozen=True)
class AwaitingApproval:
    """An open proposal the user has not approved: no approval is recorded and its stage is not active."""


@dataclass(frozen=True)
class InProgress:
    """An open proposal approved for implementation, or started, and not yet built."""


ProposalProgress = AwaitingApproval | InProgress


def proposal_progress(issue: Issue) -> ProposalProgress:
    """In progress when the approval is recorded, or when no answer is recorded and the stage is active."""
    match decision_answer(issue):
        case ApprovedDecision():
            return InProgress()
        case UnansweredDecision() if issue.value("stage") == IN_PROGRESS_STAGE:
            return InProgress()
        case _:
            return AwaitingApproval()


def review_is_done(issue: Issue) -> bool:
    """Use the same closed states as the Review page."""
    return (issue.value("status") != "open" or isinstance(proposal_progress(issue), InProgress)
            or isinstance(decision_answer(issue), DeclinedDecision) or review_mark(issue) == ReviewMark.DONE)


def _approved_at_backlog(issue: Issue) -> bool:
    """An open proposal whose approval is recorded and whose stage is still the one it was filed at."""
    return (issue.value("status") == "open" and PROPOSAL_TAG in issue.fields.get("tags", [])
            and isinstance(decision_answer(issue), ApprovedDecision) and issue.value("stage") == "backlog")


@dataclass(frozen=True)
class Moved:
    """The issue's stage is now active, or would be on a dry run."""
    path: Path


@dataclass(frozen=True)
class LeftAlone:
    """Another writer changed the issue after the folder read, and it no longer qualifies."""
    path: Path


@dataclass(frozen=True)
class MoveRefused:
    """The issue file cannot be edited. The run goes on with the other issues."""
    path: Path
    reason: str


MoveOutcome = Moved | LeftAlone | MoveRefused


def _move_one(path: Path, today: date, *, dry_run: bool) -> MoveOutcome:
    """Set stage active on the issue when it still qualifies under the lock.

    A dry run makes the same change, so it refuses the same files, and does not write it.
    """
    with issue_lock(path):
        if not _approved_at_backlog(Issue(path, frontmatter(path))):
            return LeftAlone(path)
        try:
            text, _ = _clear_waiting_mark(read_issue(path), path)
            text, _ = set_fields(text, {"stage": IN_PROGRESS_STAGE, "date_modified": _vault_date(today)}, path)
            if not dry_run:
                write_issue(path, text)
        except (ValueError, OSError, UnicodeError) as error:
            return MoveRefused(path, str(error))
        return Moved(path)


def move_approved(folder: Path, today: date, *, dry_run: bool) -> Iterator[MoveOutcome]:
    """Move each open proposal in the folder whose approval is recorded and whose stage is backlog to stage active.

    Gives each outcome as it is written, or as it would be on a dry run. A second run moves none.
    """
    if not folder.is_dir():
        raise Refusal(f"{folder} is not a folder")
    for issue in proposal_store_issues(folder):
        if PROPOSAL_TAG in issue.fields.get("tags", []) and _approved_at_backlog(issue):
            yield _move_one(issue.path, today, dry_run=dry_run)


def area_style(folder: Path, today: date, *, dry_run: bool) -> Iterator[Path]:
    """Set the style area on current and legacy style issues with no area or the unfiled area."""
    if not folder.is_dir():
        raise Refusal(f"{folder} is not a folder")
    for issue in proposal_store_issues(folder):
        path = issue.path
        with issue_lock(path):
            fields = frontmatter(path)
            if not ({"style_rule", "style_rules"} & fields.keys()):
                continue
            text = read_issue(path)
            lines = record_store.lines_with_endings(text)
            span = _key_span(lines, _closing_fence(lines, path), "area")
            if span is not None and re.fullmatch(
                r"area:\s*(?:\"\[\[unfiled\]\]\"|'\[\[unfiled\]\]'|\[\[unfiled\]\])\s*",
                lines[span[0]],
            ) is None:
                continue
            if span is None:
                text, _ = set_fields(text, {"area": f"[[{STYLE_AREA}]]"}, path)
            else:
                start = span[0]
                newline = lines[start][len(lines[start].rstrip("\r\n")):] or "\n"
                lines[start] = _value_line("area", f"[[{STYLE_AREA}]]")[:-1] + newline
                text = "".join(lines)
            text, _ = set_fields(text, {"date_modified": _vault_date(today)}, path)
            if not dry_run:
                write_issue(path, text)
        yield path


def _generated_span(text: str, path: Path) -> tuple[int, int]:
    start, end = text.find(GENERATED_START), text.find(GENERATED_END)
    if start < 0 or end < start:
        raise Refusal(f"{path}: missing generated decision body")
    return start, end + len(GENERATED_END)


def require_generated_body(path: Path) -> None:
    """Refuse a decision that a refresh cannot write."""
    _ = _generated_span(read_issue(path), path)


def refresh_decision(path: Path, count: int, body: str, ranking: str, today: date) -> list[str]:
    """Replace the generated body and style_count, and the ranking while the decision is unanswered.

    Write the file only when its bytes change.
    """
    if count < 0:
        raise Refusal("refresh requires a nonnegative count")
    with issue_lock(path):
        text = read_issue(path)
        start, end = _generated_span(text, path)
        head = text[:start]
        if isinstance(decision_answer(Issue(path, frontmatter(path))), UnansweredDecision):
            head = PROPOSED_RANKING.sub(lambda _: f"Proposed ranking: {ranking}", head, count=1)
        updated = head + decision_body(body).rstrip("\n") + text[end:]
        updated, changed = set_fields(updated, {"style_count": str(count)}, path)
        if updated == text:
            return []
        updated, dates = set_fields(updated, {"date_modified": _vault_date(today)}, path)
        write_issue(path, updated)
        return ["body", *changed, *dates]


def fill_ranking(path: Path, ranking: str) -> bool:
    """Fill an empty Proposed ranking line. Write the file only when its bytes change; True when it does."""
    with issue_lock(path):
        text = read_issue(path)
        updated = EMPTY_RANKING.sub(lambda _: f"Proposed ranking: {ranking}", text, count=1)
        if updated == text:
            return False
        write_issue(path, updated)
        return True


def _same_decision(path: Path, keys: Mapping[str, str], stores: DecisionStores, *,
                   strict: bool, local: bool = True) -> bool:
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return False
    except OSError:
        if strict:
            raise
        return False
    if not stat.S_ISREG(mode):
        return False
    fields = record_store.read_header(path, keys=frozenset(keys) | {"production_id", "proposed_by"}, strict=strict)
    issue = Issue(path, fields)
    return all(issue.value(key) == value for key, value in keys.items()) and stores.owns(issue, local=local)


def _existing_decision(path: Path, keys: Mapping[str, str], records: Iterable[Path],
                       roots: tuple[Path, ...], stores: DecisionStores, *, strict: bool) -> Path | None:
    seen: set[Path] = set()
    for candidate in chain((path,), records):
        seen.add(candidate)
        if _same_decision(candidate, keys, stores, strict=strict):
            return candidate
    return next((candidate for candidate in stores.former_paths(roots, strict=strict) if candidate not in seen
                 and _same_decision(candidate, keys, stores, strict=strict, local=False)), None)


@dataclass(frozen=True)
class DecisionNotFiled:
    requested: Path


def decision_file(path: Path, *, rule: str, crate: str, rule_blob: str, owner: staff.StaffId | None = None) -> Path | DecisionNotFiled:
    """Find the decision by its key, even if another proposal uses the requested filename."""
    keys = {"style_rule": rule, "style_crate": crate, "style_rule_blob": rule_blob}
    members = staff.snapshot()
    owner = _proposal_owner(path.parent, "", members) if owner is None else owner
    roots = _proposal_folders(path.parent, owner, members=members)
    stores = DecisionStores(owner, members, strict=False)
    return (_existing_decision(path, keys, record_store.stored_paths(roots), roots, stores, strict=False)
            or DecisionNotFiled(path))


def create(path: Path, project: str, short: str, proposed_by: str, tags: list[str],
           rule: str, crate: str, rule_blob: str, count: int, body: str, template: Path,
           today: date, proposed_ranking: str = "", rule_urls: tuple[str, ...] = (), *,
           initial_fields: Mapping[str, str], filed_at: Callable[[Path], None] = lambda _path: None,
           result_file: Path | None = None, owner: staff.StaffId | None = None) -> bool:
    """Create a decision once, identified by its rule, crate and rule text."""
    keys = {"style_rule": rule, "style_crate": crate, "style_rule_blob": rule_blob}
    if not all(keys.values()) or count < 1:
        raise Refusal("create requires a rule, crate, rule blob and positive count")
    inherited = dict(initial_fields)
    if any(key not in INHERITED_FIELDS for key in inherited):
        raise Refusal("invalid inherited fields: " + ", ".join(sorted(set(inherited) - INHERITED_FIELDS)))
    identities: tuple[tuple[str, staff.Kind], ...] = (("unit", "unit"), ("approved_in", "showrunner"))
    for key, kind in identities:
        if inherited.get(key):
            inherited[key] = staff_reference(inherited[key], kind).id
    if result_file is not None and (result_file.exists() or result_file.is_symlink()):
        raise FileExistsError(f"The result file already exists: {result_file}")
    with _folder_lock(path.parent), proposal_numbers() as numbers:
        members = staff.snapshot()
        if owner is None:
            owner = _proposal_owner(path.parent, proposed_by, members, strict=True)
        else:
            production = members.by_id(owner)
            if production is None or production.kind not in ("showrunner", "producer"):
                raise Refusal(f"{owner}: no production staff row")
        folders = _proposal_folders(path.parent, owner, strict=True, members=members, require_existing_document=True)
        records = tuple(record_store.stored_paths(folders, strict=True))
        stores = DecisionStores(owner, members, strict=True)
        if (existing := _existing_decision(path, keys, records, folders, stores, strict=True)) is not None:
            with _creation_result(result_file, existing):
                pass
            filed_at(existing)
            return False
        draft = _decision_text(path, template, project, short, proposed_by, tags, keys, count, body, today,
                               proposed_ranking, rule_urls, inherited)
        names = {record.name for record in records}
        identity = numbers.next_id(*folders, covered_owner=owner)
        while True:
            destination = _free_proposal_path(path, identity, names)
            text, _ = set_fields(draft, {NUMBER_FIELD: identity, "production_id": owner}, destination)
            try:
                with (_creation_result(result_file, destination),
                      numbers.write(destination, identity, owner, new_record=True)):
                    record_store.create_record_text(destination, text)
            except record_store.OccupiedRecordPath:
                if _same_decision(destination, keys, stores, strict=True):
                    with _creation_result(result_file, destination):
                        pass
                    filed_at(destination)
                    return False
                names.add(destination.name)
                identity = numbers.next_id_after_collision(identity, destination)
                continue
            filed_at(destination)
            return True


@contextmanager
def _creation_result(result_file: Path | None, destination: Path) -> Generator[None]:
    """Publish the result path before the record and restore the result on failure."""
    if result_file is None:
        yield
        return
    if result_file.resolve() == destination.resolve():
        raise Refusal("The result file must differ from the Proposal file.")
    with atomic_file.prepared_replacement(result_file, str(destination).encode("utf-8"), mode=0o600) as temporary:
        os.link(temporary, result_file)
    try:
        atomic_file.sync_folder(result_file.parent)
        yield
    except Exception:
        result_file.unlink()
        atomic_file.sync_folder(result_file.parent)
        raise


def _decision_text(path: Path, template: Path, project: str, short: str, proposed_by: str, tags: list[str],
                   keys: dict[str, str], count: int, body: str, today: date, proposed_ranking: str,
                   rule_urls: tuple[str, ...], inherited: dict[str, str]) -> str:
    """Validate the template and render the decision before numbering it."""
    text = template.expanduser().read_text(encoding="utf-8")
    try:
        proposer = staff_reference(proposed_by, "showrunner")
    except Refusal:
        proposer = staff_reference(proposed_by, "unit")
    values = {
        "AREA": STYLE_AREA, "PROJECT": project, "SHORT": short,
        "PROPOSED_BY": proposer.id,
        "DATE": today.isoformat(), "CATEGORY": "refactor", "DEPENDS_ON": "[]",
    }
    text = re.sub(r"\{\{([A-Z_]+)\}\}", lambda match: values.get(match[1], ""), text)
    lists = {"tags": list(dict.fromkeys(["issue", "proposal", *tags]))}
    if rule_urls:
        lists["style_rules"] = sorted(set(rule_urls))
    text, _ = set_list_fields(text, lists, path)
    # An inherited approval starts in progress, by the rule decision_answer reads.
    approved = isinstance(decision_answer(Issue(path, {key: [value] for key, value in inherited.items()})), ApprovedDecision)
    stage = {"stage": IN_PROGRESS_STAGE} if approved else {}
    text, _ = set_fields(text, {"status": "open", "stage": "backlog", **inherited, **stage, **keys,
                                "style_count": str(count)}, path)
    text, _ = remove_fields(text, REMOVED_FIELDS, path)
    return text.rstrip() + f"\n\nProposed ranking: {proposed_ranking}\n\n" + decision_body(body)


def _initial_fields(raw: str) -> dict[str, str]:
    decoded = cast(object, json.loads(raw))
    if not isinstance(decoded, dict):
        raise Refusal("--initial-fields takes a JSON object of text fields")
    fields: dict[str, str] = {}
    for key, value in cast(dict[str, object], decoded).items():
        if not isinstance(value, str):
            raise Refusal(f"--initial-fields: {key} is not text")
        fields[key] = value
    return fields


def _main_move_approved(folder: Path, today: date, *, dry_run: bool) -> int:
    moved = 0
    refused = False
    try:
        for outcome in move_approved(folder, today, dry_run=dry_run):
            match outcome:
                case Moved(path=path):
                    moved += 1
                    print(f"move-approved {path.name}: stage {IN_PROGRESS_STAGE}", flush=True)
                case MoveRefused(path=path, reason=reason):
                    refused = True
                    print(f"move-approved {path.name}: refused {reason}", flush=True)
                case LeftAlone():
                    pass
    except (ValueError, OSError, UnicodeError) as error:
        print(f"proposal_issue: {error}", file=sys.stderr)
        return 2
    print(f"move-approved: {moved} moved")
    return 2 if refused else 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    verbs = parser.add_subparsers(dest="verb", required=True)
    approving = verbs.add_parser("approve", help="record the approval; stage active (in progress)")
    _ = approving.add_argument("--by", required=True, choices=APPROVERS,
                             help="user when the user approved, showrunner when a showrunner approved without the user")
    _ = approving.add_argument("--unit", required=True, help="the unit, as its staff row names it")
    unapproving = verbs.add_parser("unapprove", help="set an approved proposal back to waiting for approval")
    _ = unapproving.add_argument("--dry-run", action="store_true", help="print the change and write nothing")
    _ = verbs.add_parser("start", help="stage active")
    close = verbs.add_parser("close", help="set as_built and close")
    closure = close.add_mutually_exclusive_group(required=True)
    _ = closure.add_argument("--as-built", help="the as-built doc")
    _ = closure.add_argument("--reason", help="why the issue is closed")
    creation = verbs.add_parser("create", help="create a proposal from held findings")
    _ = creation.add_argument("issue", type=Path)
    for name in ("project", "short", "proposed-by", "rule", "crate", "rule-blob"):
        _ = creation.add_argument(f"--{name}", required=True)
    _ = creation.add_argument("--production", type=staff.StaffId, help="the staff id of the production that owns the decision")
    _ = creation.add_argument("--count", type=int, required=True)
    _ = creation.add_argument("--extra-tags", nargs="+", default=[])
    _ = creation.add_argument("--proposed-ranking", default="")
    _ = creation.add_argument("--rules", default="")
    _ = creation.add_argument("--initial-fields", default="{}")
    _ = creation.add_argument("--body-file", type=Path, required=True)
    _ = creation.add_argument("--filed-path-file", type=Path, help="write the actual proposal path to this file")
    _ = creation.add_argument("--template", type=Path, default=Path.home() / "rust/hanadocs/.claude/templates/issue.yaml")
    reopening = verbs.add_parser("reopen", help="reopen a built decision, keeping its answer")
    moving = verbs.add_parser("move-approved", help="move each open approved proposal at backlog to stage active")
    _ = moving.add_argument("folder", type=Path)
    _ = moving.add_argument("--dry-run", action="store_true", help="print the moves and write nothing")
    styling = verbs.add_parser("area-style", help="set the area on unfiled current and legacy style issues")
    _ = styling.add_argument("folder", type=Path)
    _ = styling.add_argument("--dry-run", action="store_true", help="print the changes and write nothing")
    answer = verbs.add_parser("decline-answer", help="store a decision's declined answer")
    _ = answer.add_argument("--words", required=True)
    linking = verbs.add_parser("rules", help="set the rule links")
    _ = linking.add_argument("--rules", required=True)
    ranking = verbs.add_parser("urgency", help="set where it goes among the proposals waiting for the user")
    _ = ranking.add_argument("--level", required=True, choices=urgency.LEVELS)
    _ = ranking.add_argument("--due", default="", help="ISO date or time; only with soon")
    important = verbs.add_parser("mark-important", help="the user called it important: it goes first")
    stars = verbs.add_parser("stars", help="set the five ranking values")
    numbering = verbs.add_parser("number", help="give each proposal without one its P number")
    _ = numbering.add_argument("issues", type=Path, nargs="+")
    _ = stars.add_argument("--goal", required=True, help="e.g. '1 - Ship Hana'")
    for name in ("alignment", "impact", "urgency", "effort"):
        _ = stars.add_argument(f"--{name}", required=True, help="1 to 5")
    for verb in (approving, unapproving, verbs.choices["start"], close, stars, linking, reopening, answer, ranking, important):
        _ = verb.add_argument("issue", type=Path)
    args = parser.parse_args(argv)
    verb = cast(str, args.verb)
    today = datetime.now(user_zone()).date()
    if verb == "number":
        try:
            print("\n".join(number([path.expanduser() for path in cast(list[Path], args.issues)], today)))
        except (ValueError, OSError, UnicodeError) as error:
            print(f"proposal_issue: {error}", file=sys.stderr)
            return 2
        return 0
    if verb == "move-approved":
        return _main_move_approved(cast(Path, args.folder).expanduser(), today, dry_run=cast(bool, args.dry_run))
    if verb == "area-style":
        try:
            for changed_path in area_style(cast(Path, args.folder).expanduser(), today, dry_run=cast(bool, args.dry_run)):
                print(f"area-style {changed_path.name}: area [[{STYLE_AREA}]]", flush=True)
        except (ValueError, OSError, UnicodeError) as error:
            print(f"proposal_issue: {error}", file=sys.stderr)
            return 2
        return 0
    # A plan's Issues line writes `~/rust/hanadocs/issues/<name>.md`, quoted since names hold spaces.
    path = cast(Path, args.issue).expanduser()
    try:
        if verb == "create":
            locations: list[Path] = []
            created = create(path, cast(str, args.project), cast(str, args.short), cast(str, args.proposed_by),
                             cast(list[str], args.extra_tags), cast(str, args.rule), cast(str, args.crate),
                             cast(str, args.rule_blob), cast(int, args.count),
                             cast(Path, args.body_file).read_text(), cast(Path, args.template), today,
                             cast(str, args.proposed_ranking),
                             tuple(url for url in cast(str, args.rules).split(",") if url),
                             initial_fields=_initial_fields(cast(str, args.initial_fields)), filed_at=locations.append,
                             result_file=cast(Path | None, args.filed_path_file), owner=cast(staff.StaffId | None, args.production))
            actual, = locations
            print(f"create {actual.name}: {'created' if created else 'unchanged'}")
            return 0
        if verb == "reopen":
            changed = reopen(path, today)
        elif verb == "approve":
            changed = approve(path, {"unit": cast(str, args.unit)},
                              cast(str, args.by), calling_showrunner(), today)
        elif verb == "unapprove":
            changed = unapprove(path, calling_showrunner(), today, dry_run=cast(bool, args.dry_run))
        elif verb == "decline-answer":
            changed = edit(path, {"style_answer": "declined", "style_answer_words": cast(str, args.words)}, today)
        elif verb in ("urgency", "mark-important"):
            now = datetime.now(user_zone()).replace(tzinfo=None)
            level = (urgency.mark(now) if verb == "mark-important" or cast(str, args.level) == urgency.USER
                     else urgency.read(cast(str, args.level), cast(str, args.due)))
            changed = set_urgency(path, level, today)
        elif verb == "rules":
            changed = edit_rules(path, [url for url in cast(str, args.rules).split(",") if url], today)
        else:
            changed = edit(path, fields_for(args, today), today)
    except (ValueError, OSError, UnicodeError) as error:
        print(f"proposal_issue: {error}", file=sys.stderr)
        return 2
    print(f"{verb} {path.name}: set {', '.join(changed)}" if changed else f"{verb} {path.name}: unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

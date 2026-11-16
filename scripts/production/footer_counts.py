"""Count the user's waiting items through the same staff routing as the change page."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from scripts.production import change_review
from scripts.lib import conf
from scripts.production import change_routing, staff
from scripts.production import proposals_folder
from scripts.production.issues import Issue, source_paths

if TYPE_CHECKING:
    from scripts.production.update_registration import QuestionStaff

COUNTS_HEAD = "For you: "


class FooterCounts(NamedTuple):
    changes: int = 0
    proposals: int = 0
    questions: int = 0
    efficiency: int = 0
    unrouted: int = 0
    investigations: int = 0


def total(each: list[FooterCounts]) -> FooterCounts:
    return FooterCounts(*(sum(column) for column in zip(FooterCounts(), *each)))


def change_counts(doc: Path, record: staff.StaffSnapshot, *, source: str = "",
                  sources: list[Path] | None = None) -> tuple[int, int]:
    """Read change counts through the shared review reader."""
    from scripts.review_links import review_items
    return review_items.change_counts(doc, record, source=source, sources=sources)

def changes(doc: Path) -> int:
    return change_counts(doc, staff.snapshot())[0]


def unrouted_changes(doc: Path) -> int:
    """Count the same global unrouted tab as the change page."""
    return change_counts(doc, staff.snapshot())[1]


def proposals(doc: Path, *, source: str = "", sources: list[Path] | None = None,
              own: list[Issue] | None = None) -> int:
    """Read proposal counts through the shared review reader."""
    from scripts.review_links import review_items
    return review_items.proposals(doc, source=source, sources=sources, own=own)

def open_efficiency(line: str, *, source: str = "") -> bool:
    """Read an efficiency line through the shared review reader."""
    from scripts.review_links import review_items
    return review_items.open_efficiency(line, source=source)

def efficiency_ledger(doc: Path) -> Path | None:
    """Find the ledger through the shared review reader."""
    from scripts.review_links import review_items
    return review_items.efficiency_ledger(doc)

def efficiency(doc: Path, *, source: str = "", sources: list[Path] | None = None) -> int:
    """Read efficiency counts through the shared review reader."""
    from scripts.review_links import review_items
    return review_items.efficiency(doc, source=source, sources=sources)

def read_counts(doc: Path, *, questions: int = 0, record: staff.StaffSnapshot | None = None,
                source: str = "", sources: list[Path] | None = None,
                own: list[Issue] | None = None) -> FooterCounts:
    """Read counts. A source collector requires complete reads; other callers retain available counts.

    `own` is the doc's `open_proposals` when the caller already read them.
    """
    try:
        record = record if record is not None else staff.snapshot()
        investigation_count = investigations(doc, record, source=source, sources=sources)
        change_count, unrouted_count = change_counts(doc, record, source=source, sources=sources)
        return FooterCounts(change_count, proposals(doc, source=source, sources=sources, own=own), questions,
                            efficiency(doc, source=source, sources=sources), unrouted_count, investigation_count)
    except OSError:
        if sources is not None:
            raise
        return FooterCounts(questions=questions)


def investigations(doc: Path, record: staff.StaffSnapshot, *, source: str = "",
                   sources: list[Path] | None = None) -> int:
    """Read investigation counts through the shared review reader."""
    from scripts.review_links import review_items
    return review_items.investigations(doc, record, source=source, sources=sources)

def routing_sources(record: staff.StaffSnapshot | None = None, *, sources: list[Path] | None = None) -> list[Path]:
    """Watch configuration, staff, review lists, and delivery records for count changes."""
    root = change_routing.review_root() if sources is None else change_routing.review_root(sources=sources)
    record = record if record is not None else staff.snapshot()
    lists = change_review.lists_directory() if sources is None else change_review.lists_directory(sources=sources)
    return [conf.path("change_review"), conf.path("review_links"), lists,
            *source_paths(lists, change_review.PREFIX + "*.md", sources=sources),
            *(Path(path) for path in record.inputs), root,
            *source_paths(root, ".review-link-delivery-*", sources=sources)]


def sources(doc: Path, *, identities: QuestionStaff | None = None,
            read_sources: list[Path] | None = None, own: list[Issue] | None = None) -> list[Path]:
    """Watch count sources. A supplied collector retains paths and requires complete reads.

    `own` is the doc's `open_proposals` when the caller already read them.
    """
    from scripts.production.update_registration import (QuestionFolder, QuestionStaff, RegistrationFailure,
                                     question_folder, question_records_for_document)

    found = [] if read_sources is None else read_sources
    found.append(doc)
    try:
        if identities is None:
            identities = QuestionStaff.read() if read_sources is None else QuestionStaff.read(sources=read_sources)
        listing = (change_review.scope_list(doc, record=identities.record) if read_sources is None
                   else change_review.scope_list(doc, record=identities.record, sources=read_sources))
        listed = [item.path for item in change_review.scope_lists(
            listing, doc, record=identities.record, sources=read_sources)]
    except change_review.ChangeReviewError:
        listed = []
    except (staff.StaffRefused, OSError) as error:
        if read_sources is not None:
            raise ValueError(str(error)) from error
        listed = []
    found.extend(listed)
    folder = proposals_folder.issues_folder(doc)
    if folder is not None:
        found.append(folder)
    queue = proposals_folder.ledger(doc)
    if queue is not None:
        found.append(queue)
    try:
        efficiency_queue = efficiency_ledger(doc)
    except OSError:
        if read_sources is not None:
            raise
        efficiency_queue = None
    if efficiency_queue is not None:
        found.append(efficiency_queue)
    if own is not None:
        found.extend(issue.path for issue in own)
    elif folder is not None:
        try:
            found.extend(issue.path for issue in proposals_folder.open_proposals(doc, sources=read_sources))
        except OSError:
            if read_sources is not None:
                raise
    try:
        if identities is None:
            identities = QuestionStaff.read() if read_sources is None else QuestionStaff.read(sources=read_sources)
        questions = question_folder(doc, identities)
        if isinstance(questions, QuestionFolder):
            if questions.path not in found:
                found.append(questions.path)
            found.extend(record.path for record in question_records_for_document(doc, identities, sources=read_sources)
                         if record.status == "open")
    except (staff.StaffRefused, RegistrationFailure, OSError) as error:
        if read_sources is not None:
            raise ValueError(str(error)) from error
        print(f"questions: unavailable — {error}", file=sys.stderr)
    return found


def plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def counts_line(counts: FooterCounts) -> str | None:
    """`For you: 1 question, 1 investigation, 2 changes, 130 efficiency, 2 proposals`.

    Questions come first because they need the user most (user, 2026-10-20). "efficiency" has
    no plural. Zero counts are left out; None when all are zero.
    """
    parts = [text for count, text in (
        (counts.questions, plural(counts.questions, "question")),
        (counts.investigations, plural(counts.investigations, "investigation")),
        (counts.changes, plural(counts.changes, "change")),
        (counts.efficiency, f"{counts.efficiency} efficiency"),
        (counts.proposals, plural(counts.proposals, "proposal")),
        (counts.unrouted, f"{counts.unrouted} unrouted")) if count]
    return COUNTS_HEAD + ", ".join(parts) if parts else None

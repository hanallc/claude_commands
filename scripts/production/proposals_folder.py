"""Find proposal stores and join their proposers to the production's staff members."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path


from scripts.agents.remote import local_machine
from scripts.production import record_store, staff, unit_lookup
from scripts.production import showrunner_tree
from scripts.production.issues import ALL_FIELDS, Issue
# Callers such as rename read the tag here.
from scripts.production.issues import PROPOSAL_TAG as PROPOSAL_TAG
from scripts.production.production_doc import Refusal, production_field
from scripts.production.record_store import issues_folder as issues_folder

IMPROVEMENT_QUEUE_FIELD = "Improvement queue"


def ledger(doc: Path) -> Path | None:
    """Return the configured ledger, or None when the document has no ledger field."""
    try:
        lines = doc.read_text(encoding="utf-8").splitlines()
    except UnicodeError as error:
        raise OSError(f"{doc}: {error}") from error
    try:
        return Path(production_field(lines, IMPROVEMENT_QUEUE_FIELD)).expanduser()
    except Refusal:
        return None


def owner_docs(doc: Path, *, record: staff.StaffSnapshot | None = None) -> list[Path]:
    """Return this owner's document and the documents of its descendants."""
    record = staff.snapshot() if record is None else record
    owner = unit_lookup.doc_owner(doc, record)
    docs = {row.name: row.doc.path for row in record.find(kind="showrunner")
            if isinstance(row.doc, (staff.LiveDoc, staff.WrappedDoc))}
    return [doc, *(docs[child] for child in showrunner_tree.descendants_of(owner.name, record=record) if child in docs)]


def scope_docs(doc: Path, below: bool = False, *,
               record: staff.StaffSnapshot | None = None) -> list[Path]:
    """Return the production document and the selected descendant documents."""
    return owner_docs(doc, record=record) if below else [doc]


def owners(doc: Path, below: bool = False) -> frozenset[str]:
    """Read the fixed ids of each production and its units."""
    record = staff.snapshot()
    identities: set[str] = set()
    for production in scope_docs(doc, below, record=record):
        owner = unit_lookup.doc_owner(production, record)
        identities.add(owner.id)
        identities.update(row.id for row in record.find(kind="unit", parent=owner.id))
    return frozenset(identities)


def proposer(issue: Issue) -> str:
    """Read the proposer's fixed staff id."""
    return issue.value("proposed_by")


def proposal_issues(doc: Path, *, sources: list[Path] | None = None,
                    select: Callable[[Issue], bool] | None = None, keys: frozenset[str] = ALL_FIELDS) -> list[Issue]:
    """Read open proposals, including records in former folders."""
    try:
        row = staff.snapshot().by_doc(local_machine(), doc.resolve())
    except (staff.StaffUnreadable, staff.StaffRefused, OSError):
        if sources is not None:
            raise
        row = None
    owner = row.id if row is not None else staff.StaffId("")
    roots = record_store.known_folders(record_store.RecordKind.PROPOSAL, owner, document=doc,
                                       strict=sources is not None)

    def wanted(issue: Issue) -> bool:
        return (issue.value("status") == "open" and PROPOSAL_TAG in issue.fields.get("tags", [])
                and issue.value("record_type") != "question"
                and record_store.record_owner(record_store.RecordKind.PROPOSAL, issue, owner) == owner
                and (select is None or select(issue)))

    found: list[Issue] = []
    for path in sorted(record_store.stored_paths(roots, sources=sources, strict=sources is not None)):
        fields = record_store.read_header(path, sources=sources, keys=keys | {"status", "tags", "record_type", "production_id"}, select=wanted)
        if fields:
            found.append(Issue(path, fields))
    return found


def open_proposals(doc: Path, *, sources: list[Path] | None = None) -> list[Issue]:
    """The open proposal issues in the production's folder that are its own. Staff is read only when issues exist."""
    found = proposal_issues(doc, sources=sources)
    names = owners(doc) if found else frozenset[str]()
    return [issue for issue in found if issue.value("production_id") or proposer(issue) in names]

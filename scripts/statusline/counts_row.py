"""Write session review counts.

Usage: counts_row.py <notifier instance dir>
       counts_row.py --unit <staff id>

assignment.py updates review counts when a source changes or a deadline passes.
The review cache is `<showrunner state>/counts/<staff id>`.
Its first line contains the review counts.
Its second line contains the next deadline, or an empty value.
The watched paths and optional `source=unit:<staff id>` follow.
The final line identifies the count version.
The cache time records when the read began.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import os
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.lib import conf
from scripts.hooks import showrunner_footer  # noqa: E402
from scripts.production import footer_counts, staff
from scripts.review_links import review_items, review_later
from scripts.production.update_registration import InputError, QuestionStaff, RegistrationFailure
from scripts.statusline.rebuild_row import write_status_cache as write_cache

try:
    from scripts.review_links import for_you
except Exception:
    for_you = None


# Match the count version in assignment.py.
REVIEW_COUNTS_VERSION = "review-counts=5"


def cache_path(name: str) -> Path:
    return showrunner_footer.state_root() / "counts" / name


def retry_due(sources: list[Path]) -> str:
    """Return a retry deadline, or watch the configuration when it is unusable."""
    try:
        return str(int(time.time()) + conf.read("statusline").whole("counts.retry_seconds"))
    except conf.Unusable:
        sources.append(conf.path("statusline"))
        return ""


def question_counts(owner: staff.StaffRow, zone: ZoneInfo, identities: QuestionStaff, sources: list[Path] | None,
                    source: str = "") -> tuple[int, str]:
    """Read question counts through the shared review reader."""
    return review_items.question_counts(owner, zone, identities, sources, source)


def row(instance: Path, sources: list[Path], *, identities: QuestionStaff | None = None,
        source: str = "", require_complete: bool = False) -> tuple[str, str]:
    """Return counts and sources. Cache writes require complete reads; library callers retain available counts."""
    from scripts.production import proposals_folder
    read_sources = sources if require_complete else None
    record = identities.record if identities is not None else staff.snapshot()
    member = record.by_id(staff.StaffId(instance.name.removeprefix("showrunner-")))
    if member is not None and member.kind == "producer":
        identities = identities if identities is not None else QuestionStaff.for_record(record)
        work = review_items.question_work(member, identities, sources)
        count = review_items.ready_investigations(member.id, sources=sources)
        counts = footer_counts.FooterCounts(questions=work.for_session, investigations=count)
        return footer_counts.counts_line(counts) or "", work.deadline
    doc = showrunner_footer.production_doc(instance)
    if doc is not None:
        sources.append(doc)
    production = showrunner_footer.read_production(instance)
    if production is None:
        if not source:
            if member is None or member.kind != "showrunner":
                return "", ""
            source = member.id
        count = review_items.ready_investigations(staff.StaffId(source), sources=sources)
        return footer_counts.counts_line(footer_counts.FooterCounts(investigations=count)) or "", ""
    if identities is None:
        identities = QuestionStaff.read() if read_sources is None else QuestionStaff.read(sources=read_sources)
    own = None
    if require_complete:
        # One read of the proposal issues serves the watched paths and the count.
        own = proposals_folder.open_proposals(production.doc, sources=read_sources)
        _ = footer_counts.sources(production.doc, identities=identities, read_sources=read_sources, own=own)
    else:
        sources.extend(footer_counts.sources(production.doc, identities=identities))
    sources.extend(footer_counts.routing_sources(identities.record, sources=read_sources))
    try:
        owner = production.owner
        if identities.record.by_id(owner.id) is None:
            raise RegistrationFailure("questions", f"{production.doc}: no showrunner staff row")
    except (RegistrationFailure, staff.StaffRefused) as error:
        print(f"questions: unavailable — {error}", file=sys.stderr)
        if require_complete:
            raise showrunner_footer.FooterError(str(error)) from error
        line = footer_counts.counts_line(footer_counts.read_counts(
            production.doc, record=identities.record, source=source)) or ""
        return line, ""
    outstanding = showrunner_footer.outstanding_path(owner.id, identities=identities)
    sources.append(outstanding)
    try:
        work = review_items.question_work(owner, identities, read_sources, source)
    except (InputError, OSError, ValueError, RegistrationFailure, staff.StaffRefused) as error:
        if require_complete:
            raise showrunner_footer.FooterError(str(error)) from error
        print(f"questions: unavailable — {error}", file=sys.stderr)
        work = review_items.QuestionWork()
    counts = footer_counts.read_counts(
        production.doc, questions=work.for_session, record=identities.record, source=source, sources=read_sources,
        own=own)
    return footer_counts.counts_line(counts) or "", work.deadline


def write(instance: Path) -> Path:
    cache = cache_path(instance.name.removeprefix("showrunner-"))
    cache.parent.mkdir(parents=True, exist_ok=True)
    began = time.time_ns()
    sources = [instance / "conf", staff.record_path()]
    try:
        line, due = row(instance, sources, require_complete=True)
        sources.append(review_later.path())
        later = review_later.deadline()
        due = str(min(int(value) for value in (due,later) if value)) if due or later else ''
    except (OSError, ValueError, showrunner_footer.FooterError):
        line = ""
        due = retry_due(sources)
    write_cache(cache, line, due, sources, began, details=REVIEW_COUNTS_VERSION)
    return cache


def write_unit(identity: str) -> Path:
    """Write the unit's counts from its parent's production document."""
    cache = cache_path(identity)
    cache.parent.mkdir(parents=True, exist_ok=True)
    began = time.time_ns()
    sources = [staff.record_path()]
    try:
        record = staff.snapshot()
        unit = record.by_id(staff.StaffId(identity))
        identities = QuestionStaff.for_record(record) if unit is not None and unit.kind == "producer" else QuestionStaff.read(sources=sources)
        owner = (unit if unit is not None and unit.kind == "producer"
                 else identities.record.parent_of(unit) if unit is not None else None)
        if owner is None:
            if unit is None:
                raise ValueError(f"{identity}: no staff row")
            count = review_items.ready_investigations(unit.id, sources=sources)
            line, due = footer_counts.counts_line(footer_counts.FooterCounts(investigations=count)) or "", ""
        else:
            instance = Path(os.environ.get("NOTIFIER_STATE_DIR") or Path.home() / ".local/state/notifier") / f"showrunner-{owner.id}"
            sources.append(instance / "conf")
            line, due = row(instance, sources, identities=identities, source=identity, require_complete=True)
        sources.append(review_later.path())
        later = review_later.deadline()
        due = str(min(int(value) for value in (due,later) if value)) if due or later else ''
    except (OSError, ValueError, staff.StaffRefused, showrunner_footer.FooterError):
        line = ""
        due = retry_due(sources)
    write_cache(cache, line, due, sources, began, details=f"source=unit:{identity}\n{REVIEW_COUNTS_VERSION}")
    return cache


def main(argv: list[str]) -> int:
    if argv in (["--for-you"], ["--for-you-console"]):
        print(for_you.action_count(argv[0]) if for_you is not None else "unavailable")
        return 0
    if len(argv) == 2 and argv[0] == "--unit":
        _ = write_unit(argv[1])
        return 0
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    _ = write(Path(argv[0]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

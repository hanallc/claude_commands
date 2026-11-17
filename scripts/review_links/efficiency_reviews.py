"""Keep the Review page's log and mark for each efficiency in one file beside its ledger.

The file holds only what the ledger line has no room for, keyed by the E<n> id:
the log of the user's actions and answers, and the mark that sets the item's turn.
It never holds a copy of the ledger line.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import NotRequired, TypedDict, cast

from scripts.lib import file_lock
from scripts.lib.atomic_file import write_text_atomic
from scripts.review_links import review_events
from scripts.review_links.review_events import MarkChange, NoMark, ReviewLog, ReviewMark, StoredReviewLog


class StoredReview(TypedDict):
    log: list[StoredReviewLog]
    mark: NotRequired[str]


@dataclass(frozen=True)
class EfficiencyReview:
    log: tuple[ReviewLog, ...] = ()
    mark: ReviewMark | NoMark = NoMark()


def path(ledger: Path) -> Path:
    return ledger.with_name(ledger.stem+'.review.json')


def read(ledger: Path, sources: list[Path] | None = None) -> dict[str, EfficiencyReview]:
    target = path(ledger)
    if sources is not None:
        sources.append(target)
    try:
        stored = cast(dict[str, StoredReview], json.loads(target.read_text(encoding='utf-8')))
    except FileNotFoundError:
        return {}
    return {identity: EfficiencyReview(review_events.from_rows(entry['log']),
                                       ReviewMark(entry['mark']) if 'mark' in entry else NoMark())
            for identity, entry in stored.items()}


def record(ledger: Path, identity: str, words: str, change: MarkChange, who: str = 'nate') -> bool:
    """Append one log line and change the mark. Returns False for an exact repeat."""
    target = path(ledger)
    with file_lock.exclusive(target.with_suffix('.lock')):
        reviews = read(ledger)
        before = reviews.get(identity, EfficiencyReview())
        log = review_events.append(before.log, ReviewLog(datetime.now(timezone.utc), who, words))
        after = EfficiencyReview(log, review_events.changed_mark(before.mark, change))
        if after == before:
            return False
        reviews[identity] = after
        stored: dict[str, StoredReview] = {}
        for key, review in reviews.items():
            entry: StoredReview = {'log': review_events.rows(review.log)}
            if isinstance(review.mark, ReviewMark):
                entry['mark'] = review.mark.value
            stored[key] = entry
        write_text_atomic(target, json.dumps(stored, ensure_ascii=False, indent=1)+'\n')
        return True

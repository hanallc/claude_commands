"""Keep the Review page's log and mark for each efficiency in one file beside its ledger.

The file holds only what the ledger line has no room for, keyed by the E<n> id:
the log of the user's actions and answers, and the mark that sets the item's turn.
It never holds a copy of the ledger line.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable, Generator
from contextlib import contextmanager
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


@contextmanager
def ledger_lock(ledger: Path, *, check: Callable[[], object] | None = None) -> Generator[None]:
    with file_lock.exclusive(path(ledger).with_suffix('.lock')):
        if check is not None:
            _ = check()
        yield


def stored_reviews(target: Path) -> dict[str, object]:
    try:
        stored = cast(object, json.loads(target.read_text(encoding='utf-8')))
    except FileNotFoundError:
        return {}
    if not isinstance(stored, dict):
        raise ValueError('Expected an object of efficiency reviews.')
    return cast(dict[str, object], stored)


def decode(entry: object) -> EfficiencyReview:
    entry = cast(StoredReview, entry)
    log = review_events.from_rows(entry['log'])
    for event in log:
        _ = event.who.encode('utf-8'), event.words.encode('utf-8')
    return EfficiencyReview(log, ReviewMark(entry['mark']) if 'mark' in entry else NoMark())


def read(ledger: Path, sources: list[Path] | None = None, *,
         skipped: Callable[[Path, str, Exception], None] | None = None) -> dict[str, EfficiencyReview]:
    target = path(ledger)
    if sources is not None:
        sources.append(target)
    reviews: dict[str, EfficiencyReview] = {}
    for identity, entry in stored_reviews(target).items():
        try:
            reviews[identity] = decode(entry)
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            if skipped is not None:
                skipped(target, identity, error)
            else:
                logging.getLogger(__name__).warning('Review source %s (%s): %s', target, identity, error)
    return reviews


def record(ledger: Path, identity: str, words: str | tuple[str, ...], change: MarkChange, who: str = 'nate',
           *, check: Callable[[], object] | None = None) -> bool:
    """Append one log line and change the mark. Returns False for an exact repeat."""
    target = path(ledger)
    with ledger_lock(ledger, check=check):
        stored = stored_reviews(target)
        before = decode(stored[identity]) if identity in stored else EfficiencyReview()
        log = before.log
        for line in (words,) if isinstance(words, str) else words:
            log = review_events.append(log, ReviewLog(datetime.now(timezone.utc), who, line))
        after = EfficiencyReview(log, review_events.changed_mark(before.mark, change))
        if after == before:
            return False
        entry: StoredReview = {'log': review_events.rows(after.log)}
        if isinstance(after.mark, ReviewMark):
            entry['mark'] = after.mark.value
        stored[identity] = entry
        write_text_atomic(target, json.dumps(stored, indent=1)+'\n')
        return True

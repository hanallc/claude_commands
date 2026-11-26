"""Write the Review page log and mark of a proposal or an efficiency in its own store."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from scripts.lib.user_zone import user_zone
from scripts.production import proposal_issue
from scripts.production.issues import Issue, frontmatter
from scripts.review_links import efficiency_reviews, review_items
from scripts.review_links.review_events import MarkChange, NoMark, ReviewMark


class NoOpenQuestion(ValueError):
    """The owner answered an item that waits for no answer."""


def efficiency_identity(item: review_items.ReviewItem) -> str:
    return item.id.rsplit('/',1)[-1]


def record(item: review_items.ReviewItem, words: str, change: MarkChange, who: str = 'nate', *,
           waiting_why: str = '', waiting_only: bool = False,
           check_content: Callable[[], None] = lambda: None) -> bool:
    """Write one log line and the mark in the proposal issue or the efficiency review file."""
    def check_state() -> None:
        if waiting_only:
            require_current_waiting(item)
        check_content()

    source = item.source
    if isinstance(source, review_items.ProposalSource):
        return proposal_issue.record_review(source.record.path,words,change,datetime.now(user_zone()).date(),who,
                                            waiting_why=waiting_why, check_state=check_state)
    assert isinstance(source, review_items.EfficiencySource)
    return efficiency_reviews.record(source.path,efficiency_identity(item),words,change,who,
                                     waiting_why=waiting_why, check_state=check_state)


def require_current_waiting(item: review_items.ReviewItem) -> None:
    """Check the current canonical record while its writer holds the lock."""
    source = item.source
    if isinstance(source, review_items.ProposalSource):
        current = Issue(source.record.path, frontmatter(source.record.path))
        number = source.record.value(proposal_issue.NUMBER_FIELD)
        same_record = current.value(proposal_issue.NUMBER_FIELD) == number if number else current == source.record
        state = review_items.proposal_state(current)
    else:
        assert isinstance(source, review_items.EfficiencySource)
        identity = efficiency_identity(item)
        lines = source.path.read_text(encoding='utf-8').splitlines()
        index = int(identity.removeprefix('E')) - 1
        line = lines[index] if 0 <= index < len(lines) else ''
        same_record = line == source.line
        review = efficiency_reviews.read(source.path).get(identity, efficiency_reviews.EfficiencyReview())
        state = review_items.efficiency_state(line.partition(' — ')[2], review)
    if state != review_items.ReviewState.WAITING:
        raise NoOpenQuestion('The item is not Waiting.')
    if not same_record:
        raise NoOpenQuestion('The selected item changed.')


def stored_mark(item: review_items.ReviewItem) -> ReviewMark | NoMark:
    source = item.source
    if isinstance(source, review_items.ProposalSource):
        return proposal_issue.review_mark(source.record)
    assert isinstance(source, review_items.EfficiencySource)
    return efficiency_reviews.read(source.path).get(efficiency_identity(item),efficiency_reviews.EfficiencyReview()).mark


def _prepare_content_check(item: review_items.ReviewItem) -> Callable[[], None]:
    source = item.source
    if not isinstance(source, review_items.ProposalSource) or source.record.value(proposal_issue.NUMBER_FIELD):
        return lambda: None

    path = source.record.path
    original = path.read_bytes()

    def check() -> None:
        if path.read_bytes() != original:
            raise NoOpenQuestion('The selected item changed.')

    return check


def prepare_answer(item: review_items.ReviewItem, words: str, who: str) -> Callable[[], bool]:
    """Capture the unnumbered proposal before review and return the function that saves the answer."""
    check_content = _prepare_content_check(item)
    line = 'Answer: '+words

    def save() -> bool:
        if stored_mark(item) != ReviewMark.WAITING:
            if item.log and item.log[-1].words == line:
                return False
            raise NoOpenQuestion('this item has no open question')
        return record(item, line, NoMark(), who, waiting_only=True, check_content=check_content)

    return save

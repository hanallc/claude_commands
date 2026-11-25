"""Write the Review page log and mark of a proposal or an efficiency in its own store."""
from __future__ import annotations

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
           waiting_why: str = '', waiting_only: bool = False) -> bool:
    """Write one log line and the mark in the proposal issue or the efficiency review file."""
    def check_state() -> None:
        if waiting_only:
            require_current_waiting(item)

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
        state = review_items.proposal_state(Issue(source.record.path, frontmatter(source.record.path)))
    else:
        assert isinstance(source, review_items.EfficiencySource)
        identity = efficiency_identity(item)
        lines = source.path.read_text(encoding='utf-8').splitlines()
        index = int(identity.removeprefix('E')) - 1
        result = lines[index].partition(' — ')[2] if 0 <= index < len(lines) else ''
        review = efficiency_reviews.read(source.path).get(identity, efficiency_reviews.EfficiencyReview())
        state = review_items.efficiency_state(result, review)
    if state != review_items.ReviewState.WAITING:
        raise NoOpenQuestion('The item is not Waiting.')


def stored_mark(item: review_items.ReviewItem) -> ReviewMark | NoMark:
    source = item.source
    if isinstance(source, review_items.ProposalSource):
        return proposal_issue.review_mark(source.record)
    assert isinstance(source, review_items.EfficiencySource)
    return efficiency_reviews.read(source.path).get(efficiency_identity(item),efficiency_reviews.EfficiencyReview()).mark


def answer(item: review_items.ReviewItem, words: str, who: str) -> bool:
    """Return the item to the user with the owner's answer; an exact repeat changes nothing."""
    line = 'Answer: '+words
    if stored_mark(item) != ReviewMark.WAITING:
        if item.log and item.log[-1].words == line:
            return False
        raise NoOpenQuestion('this item has no open question')
    return record(item, line, NoMark(), who, waiting_only=True)

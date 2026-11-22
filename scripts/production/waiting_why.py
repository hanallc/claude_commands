"""List missing Waiting reasons and store a reason in the item's own record."""
from __future__ import annotations

try:
    with open(__file__[:__file__.rfind('/') + 1] + '../lib/package_root.py') as _package_root:
        exec(_package_root.read(), {'__file__': _package_root.name})
except FileNotFoundError:
    pass

import argparse
import sys
from pathlib import Path
from typing import cast

from scripts.production import change_review, investigation, staff, update_registration
from scripts.production.production_doc import Refusal
from scripts.review_links import decision_store, review_events, review_items, review_link


def missing(root: Path, members: staff.StaffSnapshot) -> list[str]:
    """Read missing reasons without allocating IDs or changing records."""
    items = review_items.read(root, members=members, show='Waiting')
    return [' | '.join((item.key, item.type.value, ' '.join(item.title.split()),
                        'filed by ' + (review_items.name(item.source_unit, members) if item.source_unit else 'not recorded')))
            for item in sorted(items, key=lambda item: item.key) if not item.waiting_why]


def store(item: review_items.ReviewItem, text: str) -> None:
    """Use each filer's writer and refuse items outside Waiting."""
    reason = review_events.require_waiting_why(text)
    if item.state != review_items.ReviewState.WAITING:
        raise ValueError('The item is not Waiting.')
    source = item.source
    if isinstance(source, (review_items.QuestionSource, review_items.UnownedQuestionSource)):
        update_registration.set_waiting_why(source.record, reason)
    elif isinstance(source, review_items.InvestigationSource):
        investigation.set_waiting_why(source.record.owner, source.record.id, reason)
    elif isinstance(source, (review_items.ProposalSource, review_items.EfficiencySource)):
        _ = decision_store.record(item, 'Waiting reason: ' + reason, review_events.KeepMark(),
                                  waiting_why=reason, waiting_only=True)
    else:
        raise ValueError('The item has no writable record.')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument('--root', type=Path)
    commands = parser.add_subparsers(dest='command', required=True)
    _ = commands.add_parser('list')
    setter = commands.add_parser('set')
    _ = setter.add_argument('--item', required=True)
    _ = setter.add_argument('--text', required=True)
    args = parser.parse_args(argv)
    try:
        root = cast(Path | None, args.root) or review_link.review_root()
        members = staff.snapshot()
        if cast(str, args.command) == 'list':
            for line in missing(root, members):
                print(line)
        else:
            key = cast(str, args.item)
            found = [item for item in review_items.read(root, members=members) if item.key == key]
            if len(found) != 1:
                raise ValueError('The item key must name one Review item.')
            store(found[0], cast(str, args.text))
        return 0
    except (ValueError, OSError, change_review.ChangeReviewError, update_registration.InputError,
            update_registration.RegistrationFailure, Refusal) as error:
        print('waiting_why: ' + ' '.join(str(error).split()), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

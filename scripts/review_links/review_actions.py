"""Apply Review actions through the existing stores and message sender."""
from __future__ import annotations

import secrets
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from scripts.lib.user_zone import user_zone
from scripts.production import change_review, investigation, update_registration
from scripts.review_links import change_page, review_items, review_later


def record_action(item: review_items.ReviewItem, words: str) -> None:
    source = item.source
    if isinstance(source, review_items.QuestionSource):
        update_registration.record_review_log(source.record.path,words)
    elif isinstance(source, review_items.InvestigationSource):
        investigation.record_review_log(source.record.owner,source.record.id,words)
    elif isinstance(source, review_items.ChangeSource):
        assert isinstance(source.assignment.production, Path)
        change_review.record_review_log(source.path,source.row['#'],words,source.assignment.production)


def later(item: review_items.ReviewItem, value: str, words: str) -> str:
    until = datetime.fromisoformat(value)
    if until.tzinfo is None:
        until = until.replace(tzinfo=user_zone())
    review_later.save(item.key,until)
    record_action(item,'Later until '+until.isoformat()+(': '+words if words else ''))
    return 'later until '+until.strftime('%Y-%m-%d %H:%M %Z')


def investigation_action(item: review_items.ReviewItem, action: str, words: str,
                         status: change_page.DeliveryStatus) -> tuple[change_page.MessageDelivery | change_page.NoMessageDelivery,str]:
    source = item.source
    assert isinstance(source,review_items.InvestigationSource)
    record = source.record
    if action == 'read':
        _ = investigation.mark_read(record.owner,record.id,words)
        return change_page.NoMessageDelivery(),'marked read'
    message = f'Review {item.id}: '+('Ask question' if action == 'question' else 'feedback sent')+'\n'+words
    if action == 'question':
        message += '\nAnswer with this command:\n'+change_page.reply_command(item.key)
    path = investigation.folder(record.owner)/(record.id+'.md')
    saved: change_page.StoredReviewMessage = {'saved':False,'source':str(path),'owner':item.owner,'message':message}
    page = change_page.ReviewMessagePage(path,path.parent,(record.id,),'',saved)
    with status.locked():
        attempt = secrets.token_hex()
        intent = change_page.DeliveryIntent(attempt,'verdict',recipients=(item.owner,))
        status.write_slots(page,record.id,change_page.DeliverySlots(change_page.NoDelivery(),intent))
        status.request_attempts[attempt] = change_page.AttemptOwnership(status.path(page,record.id),'claimed')
        try:
            _ = investigation.review_action(record.owner,record.id,action,words)
            page = replace(page,saved={**saved,'saved':True})
            status.write_slots(page,record.id,status.slots(page,record.id))
        except BaseException:
            status.clear_locked(page,record.id,status.slots(page,record.id),'verdict')
            raise
    return change_page.MessageDelivery(page,record.id,(item.owner,),message,attempt,(item.owner,)),(
        'question sent' if action == 'question' else 'feedback sent')



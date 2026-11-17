"""Apply Review actions through the existing stores and message sender."""
from __future__ import annotations

import secrets
import shlex
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from scripts.lib.user_zone import user_zone
from scripts.nightly_review import candidates
from scripts.production import change_review, change_routing, investigation, proposal_issue, staff, update_registration
from scripts.review_links import change_page, decision_store, efficiency_reviews, review_items, review_later
from scripts.review_links.review_events import KeepMark, MarkChange, NoMark, ReviewMark

PY = '~/.claude/scripts/lib/py ~/.claude/scripts/'


def record_action(item: review_items.ReviewItem, words: str) -> None:
    source = item.source
    if isinstance(source, (review_items.QuestionSource, review_items.UnownedQuestionSource)):
        update_registration.record_review_log(source.record.path,words)
    elif isinstance(source, review_items.InvestigationSource):
        investigation.record_review_log(source.record.owner,source.record.id,words)
    elif isinstance(source, review_items.ChangeSource):
        assert isinstance(source.assignment.production, Path)
        change_review.record_review_log(source.path,source.row['#'],words,source.assignment.production)
    elif isinstance(source, (review_items.ProposalSource, review_items.EfficiencySource)):
        _ = decision_store.record(item,words,KeepMark())


def later(item: review_items.ReviewItem, value: str, words: str) -> str:
    if isinstance(item.source, review_items.ChangeSource) and isinstance(
            item.source.assignment.production, change_routing.UnavailableProduction):
        raise change_page.AnswerRefused("the production document is not available")
    until = datetime.fromisoformat(value)
    if until.tzinfo is None:
        until = until.replace(tzinfo=user_zone())
    record_action(item,'Later until '+until.isoformat()+(': '+words if words else ''))
    review_later.save(item.key,until)
    return 'later until '+until.strftime('%Y-%m-%d %H:%M %Z')


def send(item: review_items.ReviewItem, path: Path, row: str, message: str, save: Callable[[], object],
         status: change_page.DeliveryStatus) -> change_page.MessageDelivery:
    """Save the action and its message under one delivery claim, then deliver it to the owner."""
    saved: change_page.StoredReviewMessage = {'saved':False,'source':str(path),'owner':item.owner,'message':message,'row':row}
    page = change_page.ReviewMessagePage(path,path.parent,(row,),'',saved)
    with status.locked():
        attempt = secrets.token_hex()
        intent = change_page.DeliveryIntent(attempt,'verdict',recipients=(item.owner,))
        status.write_slots(page,row,change_page.DeliverySlots(change_page.NoDelivery(),intent))
        status.request_attempts[attempt] = change_page.AttemptOwnership(status.path(page,row),'claimed')
        try:
            _ = save()
            page = replace(page,saved={**saved,'saved':True})
            status.write_slots(page,row,status.slots(page,row))
        except BaseException:
            status.clear_locked(page,row,status.slots(page,row),'verdict')
            raise
    return change_page.MessageDelivery(page,row,(item.owner,),message,attempt,(item.owner,))


def message_text(item: review_items.ReviewItem, action: str, words: str) -> str:
    heading = {'question':'Ask question','feedback':'feedback sent','approve':'Approve','decline':'Decline'}[action]
    message = f'Review {item.id}: {heading}\n{item.title}'+('\n'+words if words else '')
    if action == 'question':
        message += '\nAnswer with this command:\n'+change_page.reply_command(item.key)
    return message


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
    delivery = send(item,path,record.id,message,lambda: investigation.review_action(record.owner,record.id,action,words),status)
    return delivery,('question sent' if action == 'question' else 'feedback sent')


def decision_action(item: review_items.ReviewItem, action: str, words: str,
                    status: change_page.DeliveryStatus) -> tuple[change_page.MessageDelivery | change_page.NoMessageDelivery,str]:
    """Apply Approve, Decline, Send feedback or Ask question to a proposal or an efficiency."""
    source = item.source
    if action in ('feedback','question') and not words.strip():
        raise change_page.AnswerRefused('enter your words')
    logged = {'feedback':'feedback sent','question':'question','approve':'Approve','decline':'Decline'}[action]
    logged += ': '+words if words else ''
    mark: MarkChange = ReviewMark.WAITING if action == 'question' else ReviewMark.DONE
    message = message_text(item,action,words)
    if isinstance(source, review_items.EfficiencySource):
        identity = decision_store.efficiency_identity(item)
        if action == 'decline':
            try:
                _ = candidates.record_outcome(identity,'declined','',source.path)
            except candidates.OutcomeRefused as error:
                raise change_page.AnswerRefused(str(error)) from error
            _ = decision_store.record(item,logged,NoMark())
            return change_page.NoMessageDelivery(),'declined'
        if action == 'approve':
            message += ('\n'+source.line+'\nApply the change, then record it with this command:\n'
                        +PY+f"nightly_review/candidates.py outcome {identity} accepted --applied '<repo> <sha>' "
                        +'--ledger '+shlex.quote(str(source.path)))
        delivery = send(item,efficiency_reviews.path(source.path),identity,message,
                        lambda: decision_store.record(item,logged,mark),status)
    else:
        assert isinstance(source, review_items.ProposalSource)
        issue = source.record.path
        if action == 'approve':
            message += ('\nApprove accepts the proposed ranking; the words above can change a value. Record it:\n'
                        +PY+'production/proposal_issue.py approve '+shlex.quote(str(issue))+' --by user --unit <staff id>')
        elif action == 'decline':
            message += '\nThe page recorded the decline. Close the issue with /close_issue.'

        def save() -> None:
            if action == 'decline':
                _ = proposal_issue.edit(issue,{'style_answer':'declined','style_answer_words':words},
                                        datetime.now(user_zone()).date())
            _ = decision_store.record(item,logged,mark if action != 'decline' else NoMark())
        delivery = send(item,issue,issue.stem,message,save,status)
    return delivery,{'approve':'approved, sent to '+review_items.name(item.owner,staff.snapshot()),'decline':'declined',
                     'feedback':'feedback sent','question':'question sent'}[action]

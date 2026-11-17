"""Apply Review actions through the existing stores and message sender."""
from __future__ import annotations

import re
import secrets
import shlex
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from scripts.lib import atomic_file
from scripts.lib.user_zone import user_zone
from scripts.nightly_review import candidates
from scripts.production import change_review, change_routing, investigation, proposal_issue, record_store, staff, update_registration
from scripts.production.production_doc import Production
from scripts.review_links import change_page, decision_store, efficiency_reviews, review_items, review_later
from scripts.review_links import question_record
from scripts.review_links import review_events
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


def record_superseded(item: review_items.ReviewItem, action: str) -> None:
    """Keep the displaced wait in the record and the owner's production log."""
    words = f'User chose {action} before the answer; superseded the previous wait.'
    record_action(item, words)
    scope = update_registration.question_scope(item.owner, staff.snapshot())
    if isinstance(scope, Production):
        update_registration.append_log(scope, f'Review {item.id}: '+words, None, False)


def delivery_page(item: review_items.ReviewItem, status: change_page.DeliveryStatus) -> change_page.ReviewPage:
    """Find the existing delivery record and preserve its recovery fields."""
    source = item.source
    if isinstance(source, (review_items.QuestionSource, review_items.UnownedQuestionSource, review_items.ProposalSource)):
        path = source.record.path
        row = path.stem
    elif isinstance(source, review_items.InvestigationSource):
        path = investigation.folder(source.record.owner)/(source.record.id+'.md')
        row = source.record.id
    else:
        assert isinstance(source, review_items.EfficiencySource)
        path = efficiency_reviews.path(source.path)
        row = decision_store.efficiency_identity(item)
    return status.metadata_page(change_page.ReviewPage(path, path.parent, (row,), ''))


def clear_wait(item: review_items.ReviewItem, status: change_page.DeliveryStatus) -> None:
    """Remove the old request when the new action sends no message."""
    page = delivery_page(item, status)
    with status.locked():
        empty = change_page.ReviewPage(page.review_list, page.production, page.rows, page.token)
        status.write_slots(empty, page.rows[0], change_page.DeliverySlots(change_page.NoDelivery(), change_page.NoDelivery()))


def track_question(delivery: change_page.MessageDelivery, key: str, words: str,
                   status: change_page.DeliveryStatus) -> change_page.MessageDelivery:
    """Use the shared question ID for non-change exchanges and recovered messages."""
    with status.locked():
        slots = status.slots(delivery.page, delivery.row)
        question = slots.question
        assert isinstance(question, change_page.OpenQuestion)
        identity = question.question_id
        message = delivery.message.replace(change_page.reply_command(key), change_page.reply_command(key, identity))
        page = delivery.page
        if isinstance(page, change_page.UserQuestionPage):
            page = replace(page, saved={**page.saved, 'message': message})
        else:
            assert isinstance(page, change_page.ReviewMessagePage)
            page = replace(page, saved={**page.saved, 'message': message})
        status.write_slots(page, delivery.row, replace(slots, question=replace(question, text=words)))
        return replace(delivery, page=page, message=message)


def question_action(item: review_items.ReviewItem, action: change_page.QuestionOutcome, words: str,
                    status: change_page.DeliveryStatus) -> tuple[change_page.MessageDelivery, str]:
    """Replace an open follow-up before the normal question action saves."""
    from scripts.review_links import change_browser
    source = item.source
    assert isinstance(source, review_items.QuestionSource)
    if not words.strip():
        raise question_record.AnswerRefused('enter an answer')
    identities = update_registration.QuestionStaff.read()
    scope = update_registration.question_scope(item.owner, identities.record)
    with update_registration.outstanding_lock(update_registration.outstanding_path(scope)):
        current = update_registration.selected_question_records([source.record.path], (), None)[0]
        pending = current.item.pop('followup')
        events = review_events.decode(current.item.get('review_log', ''))
        event = review_events.ReviewLog(datetime.now(user_zone()), 'nate', 'Superseded followup: '+pending['text'])
        current.item['review_log'] = review_events.encode(review_events.append(events, event))
        update_registration.write_question_record(current)
    record_superseded(item, action)
    question = change_browser.WaitingQuestion(item.key, review_items.name(item.owner, identities.record), current,
        update_registration.question_source(scope), item.owner, review_items.name(current.asker, identities.record))
    return change_browser.submit_question(question, action, words, status)


def waiting_investigation_action(item: review_items.ReviewItem, action: str, words: str) -> None:
    """Close or replace an investigation that still waits for an answer."""
    source = item.source
    assert isinstance(source, review_items.InvestigationSource)
    store = record_store.NumberedRecords(source.record.owner, 'investigations', 'I', 'investigation', investigation.InvestigationError)
    with store.locked():
        stored = store.read(source.record.id)
        fields = stored.fields
        now = datetime.now(user_zone()).isoformat()
        event = ('Marked read' if action == 'read' else 'feedback sent' if action == 'feedback' else action)
        log = review_events.decode(fields.get('review_log', ''))
        fields['review_log'] = review_events.encode(review_events.append(log,
            review_events.ReviewLog(datetime.now(user_zone()), 'nate', event+(': '+words if words else ''))))
        fields['state'] = investigation.InvestigationState.OPEN if action == 'question' else investigation.InvestigationState.READ
        fields['answered'] = '' if action == 'question' else fields['answered'] or now
        fields['read'] = '' if action == 'question' else now
        store.write(source.record.id, fields, stored.body)


def resume_efficiency(item: review_items.ReviewItem) -> None:
    """Remove the deferred state when a new action replaces it."""
    source = item.source
    assert isinstance(source, review_items.EfficiencySource)
    identity = decision_store.efficiency_identity(item)
    lines = source.path.read_text().splitlines()
    index = int(identity[1:])-1
    head, separator, result = lines[index].partition(' — ')
    lines[index] = head+separator+re.sub(r'^deferred\b', 'proposed', result, count=1)
    atomic_file.write_text_atomic(source.path, '\n'.join(lines)+'\n')


def later(item: review_items.ReviewItem, value: str, words: str) -> str:
    if isinstance(item.source, review_items.ChangeSource) and isinstance(
            item.source.assignment.production, change_routing.UnavailableProduction):
        raise question_record.AnswerRefused("the production document is not available")
    until = datetime.fromisoformat(value)
    if until.tzinfo is None:
        until = until.replace(tzinfo=user_zone())
    record_action(item,'Later until '+until.isoformat()+(': '+words if words else ''))
    review_later.save(item.key,until)
    return 'later until '+until.strftime('%Y-%m-%d %H:%M %Z')


def send(item: review_items.ReviewItem, path: Path, row: str, message: str, save: Callable[[], object],
         status: change_page.DeliveryStatus, *, question_text: str = '') -> change_page.MessageDelivery:
    """Save the action and its message under one delivery claim, then deliver it to the owner."""
    question: change_page.NoQuestion | change_page.OpenQuestion = change_page.NoQuestion()
    if question_text:
        identity = secrets.token_hex(4)
        question = change_page.OpenQuestion(identity, question_text, change_page.NoDelivery())
        message = message.replace(change_page.reply_command(item.key), change_page.reply_command(item.key, identity))
    saved: change_page.StoredReviewMessage = {'saved':False,'source':str(path),'owner':item.owner,'message':message,'row':row}
    page = change_page.ReviewMessagePage(path,path.parent,(row,),'',saved)
    with status.locked():
        previous_page = status.metadata_page(page)
        previous_slots = status.slots(page, row)
        attempt = secrets.token_hex()
        intent = change_page.DeliveryIntent(attempt,'verdict',recipients=(item.owner,))
        status.write_slots(page,row,change_page.DeliverySlots(change_page.NoDelivery(),intent,question))
        status.request_attempts[attempt] = change_page.AttemptOwnership(status.path(page,row),'claimed')
        try:
            _ = save()
            page = replace(page,saved={**saved,'saved':True})
            status.write_slots(page,row,status.slots(page,row))
        except BaseException:
            status.write_slots(previous_page, row, previous_slots)
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
    waiting = item.state == review_items.ReviewState.WAITING
    if action != 'read' and not words.strip():
        raise investigation.InvestigationError('enter your words')
    def save() -> None:
        if waiting:
            record_superseded(item, action)
            waiting_investigation_action(item, action, words)
        elif action == 'read':
            _ = investigation.mark_read(record.owner, record.id, words)
        else:
            _ = investigation.review_action(record.owner, record.id, action, words)
    if action == 'read':
        save()
        clear_wait(item, status)
        return change_page.NoMessageDelivery(),'marked read'
    message = f'Review {item.id}: '+('Ask question' if action == 'question' else 'feedback sent')+'\n'+words
    if action == 'question':
        message += '\nAnswer with this command:\n'+change_page.reply_command(item.key)
    path = investigation.folder(record.owner)/(record.id+'.md')
    delivery = send(item,path,record.id,message,save,status,question_text=words if action == 'question' else '')
    return delivery,('question sent' if action == 'question' else 'feedback sent')


def decision_action(item: review_items.ReviewItem, action: str, words: str,
                    status: change_page.DeliveryStatus) -> tuple[change_page.MessageDelivery | change_page.NoMessageDelivery,str]:
    """Apply Approve, Decline, Send feedback or Ask question to a proposal or an efficiency."""
    source = item.source
    if action in ('feedback','question') and not words.strip():
        raise question_record.AnswerRefused('enter your words')
    waiting = item.state == review_items.ReviewState.WAITING
    def supersede() -> None:
        if waiting:
            record_superseded(item, action)
            if isinstance(source, review_items.EfficiencySource):
                resume_efficiency(item)
    logged = {'feedback':'feedback sent','question':'question','approve':'Approve','decline':'Decline'}[action]
    logged += ': '+words if words else ''
    mark: MarkChange = ReviewMark.WAITING if action == 'question' else ReviewMark.DONE
    message = message_text(item,action,words)
    if isinstance(source, review_items.EfficiencySource):
        identity = decision_store.efficiency_identity(item)
        if action == 'decline':
            supersede()
            try:
                _ = candidates.record_outcome(identity,'declined','',source.path)
            except candidates.OutcomeRefused as error:
                raise question_record.AnswerRefused(str(error)) from error
            _ = decision_store.record(item,logged,NoMark())
            clear_wait(item, status)
            return change_page.NoMessageDelivery(),'declined'
        if action == 'approve':
            message += ('\n'+source.line+'\nApply the change, then record it with this command:\n'
                        +PY+f"nightly_review/candidates.py outcome {identity} accepted --applied '<repo> <sha>' "
                        +'--ledger '+shlex.quote(str(source.path)))
        def save_efficiency() -> None:
            supersede()
            _ = decision_store.record(item, logged, mark, waiting_why=words if action == 'question' else '')
        delivery = send(item,efficiency_reviews.path(source.path),identity,message,save_efficiency,status,
                        question_text=words if action == 'question' else '')
    else:
        assert isinstance(source, review_items.ProposalSource)
        issue = source.record.path
        if action == 'approve':
            message += ('\nApprove accepts the proposed ranking; the words above can change a value. Record it:\n'
                        +PY+'production/proposal_issue.py approve '+shlex.quote(str(issue))+' --by user --unit <staff id>')
        elif action == 'decline':
            message += '\nThe page recorded the decline. Close the issue with /close_issue.'

        def save() -> None:
            supersede()
            if action == 'decline':
                _ = proposal_issue.edit(issue,{'style_answer':'declined','style_answer_words':words},
                                        datetime.now(user_zone()).date())
            _ = decision_store.record(item,logged,mark if action != 'decline' else NoMark(),
                                      waiting_why=words if action == 'question' else '')
        delivery = send(item,issue,issue.stem,message,save,status,question_text=words if action == 'question' else '')
    return delivery,{'approve':'approved, sent to '+review_items.name(item.owner,staff.snapshot()),'decline':'declined',
                     'feedback':'feedback sent','question':'question sent'}[action]

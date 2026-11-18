"""Apply Review actions through the existing stores and message sender."""
from __future__ import annotations

import re
import secrets
import shlex
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from scripts.lib import atomic_file
from scripts.lib.user_zone import user_zone
from scripts.nightly_review import candidates
from scripts.production import change_review, change_routing, investigation, proposal_issue, staff, update_registration
from scripts.production.production_doc import Production
from scripts.review_links import change_page, decision_store, efficiency_reviews, new_item_dialog, review_items, review_later, review_submissions
from scripts.review_links import question_record
from scripts.review_links import review_events
from scripts.review_links.review_events import KeepMark, MarkChange, NoMark, ReviewMark

PY = '~/.claude/scripts/lib/py ~/.claude/scripts/'


@dataclass(frozen=True)
class ScopedWait:
    key: str
    token: str = field(default_factory=new_item_dialog.new_reply_token, repr=False)


@dataclass(frozen=True)
class NoScopedWait:
    pass


NO_SCOPED_WAIT = NoScopedWait()


def record_action(item: review_items.ReviewItem, words: str, *, root: Path | None = None,
                  check: Callable[[], object] | None = None) -> None:
    source = item.source
    if isinstance(source, (review_items.QuestionSource, review_items.UnownedQuestionSource)):
        update_registration.record_review_log(source.record, words, check=check)
    elif isinstance(source, review_items.InvestigationSource):
        investigation.record_review_log(source.record.owner, source.record.id, words, check=check)
    elif isinstance(source, review_items.ChangeSource):
        production = source.assignment.production
        assert isinstance(production, Path)
        if check is not None and root is not None:
            with change_review.checked_review_write(source.path, root, check):
                change_review.record_review_log(source.path, source.row['#'], words, production)
        else:
            if check is not None:
                _ = check()
            change_review.record_review_log(source.path, source.row['#'], words, production)
    elif isinstance(source, review_items.ProposalSource):
        _ = proposal_issue.record_review(source.record.path, words, KeepMark(), datetime.now(user_zone()).date(), check=check)
    elif isinstance(source, review_items.EfficiencySource):
        _ = efficiency_reviews.record(source.path, decision_store.efficiency_identity(item), words, KeepMark(), check=check)
    elif check is not None:
        _ = check()


def record_superseded(item: review_items.ReviewItem, action: str, *,
                      scope: update_registration.QuestionScope | None = None) -> None:
    """Keep the displaced wait in the record and the owner's production log."""
    words = superseded_words(action)
    record_action(item, words)
    log_superseded(item, words, scope=scope)


def superseded_words(action: str) -> str:
    return f'User chose {action} before the answer; superseded the previous wait.'


def log_superseded(item: review_items.ReviewItem, words: str, *,
                   scope: update_registration.QuestionScope | None = None) -> None:
    if scope is None:
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
        path = source.path
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
        if isinstance(delivery.page, change_page.UserQuestionPage) and delivery.page.saved.get('question_id'):
            return delivery
        assert isinstance(question, change_page.OpenQuestion)
        identity = question.question_id
        message = delivery.message.replace(change_page.reply_command(key), change_page.reply_command(key, identity))
        original = delivery.page
        assert isinstance(original, (change_page.UserQuestionPage, change_page.ReviewMessagePage))
        page = replace(original, saved={**original.saved, 'message': message})
        updated = replace(slots, question=replace(question, text=words))
        if page != original or updated != slots:
            status.write_slots(page, delivery.row, updated)
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
    if 'review_submission' in source.record.item:
        scope = review_submissions.sent_question_scope(item.owner, identities.record)
        return sent_question_action(item, source.record, scope, action, words, status)
    scope = update_registration.question_scope(item.owner, identities.record)
    with update_registration.outstanding_lock(update_registration.outstanding_path(scope)):
        current = update_registration.current_open_question(source.record, 'followup')
        pending = current.item.pop('followup', None)
        if pending is None:
            raise review_items.ReviewRecordChanged('No follow-up waits for a reply on this question.')
        events = review_events.decode(current.item.get('review_log', ''))
        event = review_events.ReviewLog(datetime.now(user_zone()), 'nate', 'Superseded followup: '+pending['text'])
        current.item['review_log'] = review_events.encode(review_events.append(events, event))
        update_registration.write_question_record(current)
    record_superseded(item, action)
    question = change_browser.WaitingQuestion(item.key, review_items.name(item.owner, identities.record), current,
        update_registration.question_source(scope), item.owner, review_items.name(current.asker, identities.record))
    return change_browser.submit_question(question, action, words, status)


def sent_question_action(item: review_items.ReviewItem, record: update_registration.QuestionRecord,
                         scope: update_registration.QuestionScope, action: change_page.QuestionOutcome, words: str,
                         status: change_page.DeliveryStatus, *,
                         verify_revision: Callable[[], object] | None = None) -> tuple[change_page.MessageDelivery, str]:
    from scripts.review_links.change_browser import question_message

    if action not in ('answer', 'feedback', 'question'):
        raise question_record.AnswerRefused('Choose an action on this card.')
    ask = action == 'question'
    identity = record.item.get('review_id', '')
    if not identity:
        raise question_record.AnswerRefused('The sent question has no Review ID.')
    scoped = ScopedWait(review_submissions.submission_key(new_item_dialog.SubmissionKind.QUESTION, record.owner,
                            identity, new_item_dialog.read_submission(record.item.get('review_submission')).request_id)) if ask else NO_SCOPED_WAIT
    message = question_message(action, scope.name, item.title, '', words, item.key)
    document = scope.doc if isinstance(scope, Production) else record.path.parent
    saved: change_page.StoredUserAnswer = {'saved':False,'source':str(record.path),'production':str(document),
        'owner':record.owner,'question':item.title,'answer':words if action == 'answer' else '',
        'message':message,'kind':action}
    if isinstance(scoped, ScopedWait):
        saved['question_id'] = scoped.token
        saved['question_text'] = words
        saved['message'] = message.replace(change_page.reply_command(item.key),
                                           change_page.reply_command(scoped.key, scoped.token))
    page = change_page.UserQuestionPage(record.path, document, (record.path.stem,), '', saved)

    def save() -> None:
        with update_registration.outstanding_lock(update_registration.outstanding_path(scope)):
            if verify_revision is not None:
                _ = verify_revision()
            if item.state == review_items.ReviewState.WAITING:
                record_superseded(item, action, scope=scope)
            update_registration.review_sent_question(record, scope, action, words,
                                                    scoped.token if isinstance(scoped, ScopedWait) else '')

    delivery = save_delivery(page, record.owner, save, status, change_page.NoQuestion())
    return delivery, {'answer': words, 'question': 'question sent', 'feedback': 'feedback sent'}[action]


def resume_efficiency(item: review_items.ReviewItem) -> None:
    """Remove the deferred state when a new action replaces it."""
    source = item.source
    assert isinstance(source, review_items.EfficiencySource)
    identity = decision_store.efficiency_identity(item)
    with efficiency_reviews.ledger_lock(source.path):
        lines = source.path.read_text().splitlines()
        index = int(identity[1:])-1
        if index >= len(lines) or lines[index] != source.line:
            raise review_items.ReviewRecordChanged('The efficiency ledger entry changed.')
        head, separator, result = lines[index].partition(' — ')
        if result.startswith('deferred'):
            lines[index] = head+separator+re.sub(r'^deferred\b', 'proposed', result, count=1)
            atomic_file.write_text_atomic(source.path, '\n'.join(lines)+'\n')


def later(item: review_items.ReviewItem, value: str, words: str, *,
          verify_revision: Callable[[], object] | None = None, root: Path | None = None) -> str:
    if isinstance(item.source, review_items.ChangeSource) and isinstance(
            item.source.assignment.production, change_routing.UnavailableProduction):
        raise question_record.AnswerRefused("the production document is not available")
    until = datetime.fromisoformat(value)
    until = until.replace(tzinfo=user_zone()) if until.tzinfo is None else until.astimezone(user_zone())
    event = 'Later until '+until.isoformat()+(': '+words if words else '')

    def record_later(publish_deadline: Callable[[], None]) -> None:
        def check_and_publish() -> None:
            if verify_revision is not None:
                _ = verify_revision()
            publish_deadline()

        record_action(item, event, root=root, check=check_and_publish)

    # Use the same lock order as the Changes form.
    with (change_review.review_delivery_lock(root) if root is not None and isinstance(item.source, review_items.ChangeSource)
          else nullcontext()):
        review_later.save(item.key, until, record_log=record_later)
    return 'later until '+until.strftime('%Y-%m-%d %H:%M %Z')


def send(item: review_items.ReviewItem, path: Path, row: str, message: str, save: Callable[[], object],
         status: change_page.DeliveryStatus, *, question_text: str = '',
         scoped: ScopedWait | NoScopedWait = NO_SCOPED_WAIT) -> change_page.MessageDelivery:
    """Save the action and its message under one delivery claim, then deliver it to the owner."""
    question: change_page.NoQuestion | change_page.OpenQuestion = change_page.NoQuestion()
    if question_text:
        identity = scoped.token if isinstance(scoped, ScopedWait) else secrets.token_hex(4)
        key = scoped.key if isinstance(scoped, ScopedWait) else item.key
        question = change_page.OpenQuestion(identity, question_text, change_page.NoDelivery())
        message = message.replace(change_page.reply_command(item.key), change_page.reply_command(key, identity))
    saved: change_page.StoredReviewMessage = {'saved':False,'source':str(path),'owner':item.owner,'message':message,'row':row}
    page = change_page.ReviewMessagePage(path,path.parent,(row,),'',saved)
    return save_delivery(page, item.owner, save, status, question)


def prepare_submission_delivery(saved: review_submissions.SavedSubmission, words: str,
                                status: change_page.DeliveryStatus, *, evidence_url: str = ""
                                ) -> change_page.MessageDelivery | change_page.NoMessageDelivery:
    """Claim the first message for a published native request."""
    row = saved.request_id
    path = saved.original_path
    evidence = f"\n\nEvidence in Review\n{evidence_url}" if evidence_url else ""
    message = (f'New {saved.kind} {saved.id} from Review.\n\n{words}{evidence}\n\n'
               + change_page.reply_command(saved.key, saved.reply_token))
    record: change_page.StoredReviewMessage = {
        'source': str(path), 'row': row, 'saved': True, 'owner': saved.owner,
        'message': message, 'submission_request': row,
    }
    page = change_page.ReviewMessagePage(path, path.parent, (row,), '', record)
    with status.locked():
        stored = status.metadata_page(page)
        slots = status.slots(page, row)
        if isinstance(stored, change_page.ReviewMessagePage) and stored.saved.get('submission_request') == row:
            page = stored
            if page.saved.get('submission_delivered'):
                return change_page.NoMessageDelivery()
        intent = slots.verdict
        if isinstance(intent, change_page.DeliveryIntent):
            if status.owns_live_attempt(page, row, intent):
                return change_page.NoMessageDelivery()
        else:
            recipient = staff.StaffId(page.saved['owner'])
            intent = change_page.DeliveryIntent(secrets.token_hex(), 'verdict', recipients=(recipient,))
            status.write_slots(page, row, slots.updated('verdict', intent))
        status.request_attempts[intent.attempt] = change_page.AttemptOwnership(status.path(page, row), 'claimed')
    return change_page.MessageDelivery(page, row, intent.recipients, page.saved['message'], intent.attempt,
                                       (staff.StaffId(page.saved['owner']),))


def save_delivery(page: change_page.ReviewMessagePage | change_page.UserQuestionPage, owner: staff.StaffId,
                  save: Callable[[], object], status: change_page.DeliveryStatus,
                  question: change_page.NoQuestion | change_page.OpenQuestion) -> change_page.MessageDelivery:
    row = page.rows[0]
    with status.locked():
        previous_page = status.metadata_page(page)
        previous_slots = status.slots(page, row)
        attempt = secrets.token_hex()
        intent = change_page.DeliveryIntent(attempt,'verdict',recipients=(owner,))
        slots = change_page.DeliverySlots(change_page.NoDelivery(),intent,question)
        status.write_slots(page,row,slots)
        status.request_attempts[attempt] = change_page.AttemptOwnership(status.path(page,row),'claimed')
        try:
            _ = save()
        except BaseException:
            status.write_slots(previous_page, row, previous_slots)
            raise
        page = replace(page,saved={**page.saved,'saved':True})
        status.mark_review_saved(page, row, slots, intent)
    return change_page.MessageDelivery(page,row,(owner,),page.saved['message'],attempt,(owner,))


def message_text(item: review_items.ReviewItem, action: str, words: str) -> str:
    heading = {'question':'Ask question','feedback':'feedback sent','approve':'Approve','decline':'Decline'}[action]
    message = f'Review {item.id}: {heading}\n{item.title}'+('\n'+words if words else '')
    if action == 'question':
        message += '\nAnswer with this command:\n'+change_page.reply_command(item.key)
    return message


def investigation_action(item: review_items.ReviewItem, action: str, words: str,
                         status: change_page.DeliveryStatus, *,
                         verify_revision: Callable[[], object] | None = None
                         ) -> tuple[change_page.MessageDelivery | change_page.NoMessageDelivery, str]:
    source = item.source
    assert isinstance(source,review_items.InvestigationSource)
    record = source.record
    waiting = item.state == review_items.ReviewState.WAITING
    scoped = (ScopedWait(review_submissions.submission_key(new_item_dialog.SubmissionKind(record.kind.value),
                                                          record.owner, record.id, record.submission.request_id))
              if action == 'question' and isinstance(record.submission, new_item_dialog.ReviewSubmission) else NO_SCOPED_WAIT)
    if action != 'read' and not words.strip():
        raise investigation.InvestigationError('enter your words')
    def save() -> None:
        if waiting:
            if isinstance(scoped, ScopedWait):
                _ = investigation.review_action(record.owner, record.id, action, words, reply_token=scoped.token, check=verify_revision)
            else:
                investigation.change_waiting(record, action, words, check=verify_revision)
            with review_items.after_store_write(item.key):
                record_superseded(item, action)
        elif isinstance(scoped, ScopedWait):
            _ = investigation.review_action(record.owner, record.id, action, words, reply_token=scoped.token, check=verify_revision)
        elif action == 'read':
            _ = investigation.mark_read(record.owner, record.id, words, check=verify_revision)
        else:
            _ = investigation.review_action(record.owner, record.id, action, words, check=verify_revision)
    if action == 'read':
        save()
        with review_items.after_store_write(item.key):
            clear_wait(item, status)
        return change_page.NoMessageDelivery(),'marked read'
    message = f'Review {item.id}: '+('Ask question' if action == 'question' else 'feedback sent')+'\n'+words
    if action == 'question':
        message += '\nAnswer with this command:\n'+change_page.reply_command(item.key)
    path = source.path
    delivery = send(item,path,record.id,message,save,status,question_text=words if action == 'question' else '', scoped=scoped)
    return delivery,('question sent' if action == 'question' else 'feedback sent')


def decision_action(item: review_items.ReviewItem, action: str, words: str,
                    status: change_page.DeliveryStatus, *,
                    verify_revision: Callable[[], object] | None = None
                    ) -> tuple[change_page.MessageDelivery | change_page.NoMessageDelivery, str]:
    """Apply Approve, Decline, Send feedback or Ask question to a proposal or an efficiency."""
    source = item.source
    if action in ('feedback','question') and not words.strip():
        raise question_record.AnswerRefused('enter your words')
    waiting = item.state == review_items.ReviewState.WAITING
    answer = ('approved, sent to '+review_items.name(item.owner,staff.snapshot()) if action == 'approve' else
              {'decline':'declined','feedback':'feedback sent','question':'question sent'}[action])
    logged = {'feedback':'feedback sent','question':'question','approve':'Approve','decline':'Decline'}[action]
    logged += ': '+words if words else ''
    mark: MarkChange = ReviewMark.WAITING if action == 'question' else ReviewMark.DONE
    message = message_text(item,action,words)
    events = (superseded_words(action), logged) if waiting else (logged,)
    if isinstance(source, review_items.EfficiencySource):
        identity = decision_store.efficiency_identity(item)
        if action == 'decline':
            try:
                with efficiency_reviews.ledger_lock(source.path, check=verify_revision):
                    _ = candidates.record_outcome(identity,'declined','',source.path)
            except candidates.OutcomeRefused as error:
                raise question_record.AnswerRefused(str(error)) from error
            with review_items.after_store_write(item.key):
                _ = efficiency_reviews.record(source.path, identity, events, NoMark())
            if waiting:
                with review_items.after_store_write(item.key):
                    log_superseded(item, events[0])
            with review_items.after_store_write(item.key):
                clear_wait(item, status)
            return change_page.NoMessageDelivery(),answer
        if action == 'approve':
            message += ('\n'+source.line+'\nApply the change, then record it with this command:\n'
                        +PY+f"nightly_review/candidates.py outcome {identity} accepted --applied '<repo> <sha>' "
                        +'--ledger '+shlex.quote(str(source.path)))
        def save_efficiency() -> None:
            _ = efficiency_reviews.record(source.path, identity, events, mark, check=verify_revision,
                                          waiting_why=words if action == 'question' else '')
            if waiting:
                with review_items.after_store_write(item.key):
                    resume_efficiency(item)
                with review_items.after_store_write(item.key):
                    log_superseded(item, events[0])
        delivery = send(item,efficiency_reviews.path(source.path),identity,message,save_efficiency,status,
                        question_text=words if action == 'question' else '')
    else:
        assert isinstance(source, review_items.ProposalSource)
        issue = source.record.path
        origin = proposal_issue.review_submission(source.record) if action == 'question' else new_item_dialog.NoReviewSubmission()
        scoped = (ScopedWait(review_submissions.submission_key(new_item_dialog.SubmissionKind.PROPOSAL,
                             staff.StaffId(source.record.value('proposed_by')), source.record.value('proposal_id'), origin.request_id))
                  if isinstance(origin, new_item_dialog.ReviewSubmission) else NO_SCOPED_WAIT)
        if action == 'approve':
            message += ('\nApprove accepts the proposed ranking; the words above can change a value. Record it:\n'
                        +PY+'production/proposal_issue.py approve '+shlex.quote(str(issue))+' --by user --unit <staff id>')
        elif action == 'decline':
            message += '\nThe page recorded the decline. Close the issue with /close_issue.'

        def save() -> None:
            day = datetime.now(user_zone()).date()
            if action == 'decline':
                _ = proposal_issue.edit(issue, {'style_answer':'declined', 'style_answer_words':words},
                                        day, check=verify_revision)
                with review_items.after_store_write(item.key):
                    _ = proposal_issue.record_review(issue, events, NoMark(), day)
            else:
                _ = proposal_issue.record_review(issue, events, mark, day, check=verify_revision,
                                                 waiting_why=words if action == 'question' else '',
                                                 reply_token=scoped.token if isinstance(scoped, ScopedWait) else '')
            if waiting:
                with review_items.after_store_write(item.key):
                    log_superseded(item, events[0])
        delivery = send(item,issue,issue.stem,message,save,status,question_text=words if action == 'question' else '', scoped=scoped)
    return delivery,answer

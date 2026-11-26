"""Apply Review actions through the existing stores and message sender."""
from __future__ import annotations

import re
import secrets
import shlex
import sys
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from urllib.parse import urlencode

from scripts.for_you.action import REVIEW_PATH
from scripts.lib import atomic_file
from scripts.lib.user_zone import user_zone
from scripts.nightly_review import candidates
from scripts.production import change_review, change_routing, investigation, proposal_issue, staff, the_list, update_registration
from scripts.production.production_doc import Production
from scripts.review_links import change_page, decision_store, efficiency_reviews, new_item_dialog, review_items, review_later, review_submissions
from scripts.review_links import question_path, question_record, reply_tokens
from scripts.review_links.review_events import KeepMark, MarkChange, NoMark, ReviewMark

PY = '~/.claude/scripts/lib/py ~/.claude/scripts/'


@dataclass(frozen=True)
class ScopedWait:
    key: str
    proof: reply_tokens.ReplyTokenProof = field(default_factory=reply_tokens.mint)

    def command(self) -> change_page.ScopedReplyCommand:
        return {"key": self.key, "token_sha256": self.proof.sha256}


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
    """Save the action against the selected Question."""
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
    question = change_browser.WaitingQuestion(item.key, review_items.name(item.owner, identities.record), source.record,
        update_registration.question_source(scope), item.owner, review_items.name(source.record.asker, identities.record))
    return change_browser.submit_question(question, action, words, status, save=change_browser.SelectedQuestionSave())


def sent_question_action(item: review_items.ReviewItem, record: update_registration.QuestionRecord,
                         scope: update_registration.QuestionScope, action: change_page.QuestionOutcome, words: str,
                         status: change_page.DeliveryStatus, *,
                         verify_revision: Callable[[], object] | None = None) -> tuple[change_page.MessageDelivery, str]:
    from scripts.review_links.change_browser import question_message

    if action not in ('answer', 'feedback', 'question'):
        raise question_record.AnswerRefused('Choose an action on this card.')
    ask = action == 'question'
    if ask:
        words = words.strip()
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
        command = scoped.command()
        saved['question_id'] = command['token_sha256']
        saved['question_text'] = words
        saved['scoped_reply'] = command
        saved['message'] = message.replace(change_page.reply_command(item.key),
                                           change_page.reply_command(scoped.key))
    page = change_page.UserQuestionPage(record.path, document, (record.path.stem,), '', saved)

    def save() -> None:
        with update_registration.outstanding_lock(update_registration.outstanding_path(scope)):
            if verify_revision is not None:
                _ = verify_revision()
            if item.state == review_items.ReviewState.WAITING:
                record_superseded(item, action, scope=scope)
            update_registration.review_sent_question(record, scope, action, words,
                                                    scoped.proof if isinstance(scoped, ScopedWait) else reply_tokens.MINT_REPLY_PROOF)

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


def save_timed_park[Result](value: str, zone: tzinfo,
                           save: Callable[[datetime, Callable[[datetime], None]], Result], *,
                           require_offset: bool = False) -> Result:
    """Pass the selected deadline and its check to the item's writer."""
    def local_time(time: datetime) -> datetime:
        if require_offset:
            raise ValueError('a time must have a time zone')
        return time.replace(tzinfo=zone)

    until = question_path.read_later(value, local_time)
    if isinstance(until, question_path.UntilDelivered):
        raise question_record.AnswerRefused('Review Later requires a time.')

    def check_deadline(moment: datetime) -> None:
        the_list.require_active_parking(until, moment)

    return save(until, check_deadline)


def _save_review_park(item: review_items.ReviewItem, until: datetime, words: str, *,
                      check_deadline: Callable[[datetime], None],
                      verify_revision: Callable[[], object] | None, root: Path | None) -> str:
    event = 'Later until '+question_path.write_later(until)+(': '+words if words else '')

    def record_later(publish_deadline: Callable[[], None]) -> None:
        def check_and_publish() -> None:
            if verify_revision is not None:
                _ = verify_revision()
            check_deadline(datetime.now(UTC))
            publish_deadline()

        record_action(item, event, root=root, check=check_and_publish)

    # Use the same lock order as the Changes form.
    with (change_review.review_delivery_lock(root) if root is not None and isinstance(item.source, review_items.ChangeSource)
          else nullcontext()):
        review_later.save(item.key, until, record_log=record_later)
    return 'later until '+until.strftime('%Y-%m-%d %H:%M %Z')


def later(item: review_items.ReviewItem, value: str, words: str, *,
          verify_revision: Callable[[], object] | None = None, root: Path | None = None) -> str:
    if isinstance(item.source, review_items.ChangeSource) and isinstance(
            item.source.assignment.production, change_routing.UnavailableProduction):
        raise question_record.AnswerRefused("the production document is not available")
    zone = user_zone()
    return save_timed_park(value, zone, lambda until, check: _save_review_park(
        item, until.astimezone(zone), words, check_deadline=check, verify_revision=verify_revision, root=root))


def send(item: review_items.ReviewItem, path: Path, row: str, message: str, save: Callable[[], object],
         status: change_page.DeliveryStatus, *, recipient: staff.StaffId, question_text: str = '',
         scoped: ScopedWait | NoScopedWait = NO_SCOPED_WAIT) -> change_page.MessageDelivery:
    """Save the action and its message under one delivery claim, then deliver it to the recipient."""
    question: change_page.NoQuestion | change_page.OpenQuestion = change_page.NoQuestion()
    saved: change_page.StoredReviewMessage = {'saved':False,'source':str(path),'owner':recipient,'message':message,'row':row}
    if question_text:
        if isinstance(scoped, ScopedWait):
            command = scoped.command()
            saved['scoped_reply'] = command
            identity = command['token_sha256']
            reply = change_page.reply_command(scoped.key)
        else:
            identity = secrets.token_hex(4)
            reply = change_page.reply_command(item.key, identity)
        question = change_page.OpenQuestion(identity, question_text, change_page.NoDelivery())
        saved['message'] = message.replace(change_page.reply_command(item.key), reply)
    page = change_page.ReviewMessagePage(path,path.parent,(row,),'',saved)
    return save_delivery(page, recipient, save, status, question)


class SubmissionDeliveryPending(OSError):
    """Another request is sending this item."""


_READ_STAFF_AUTHORITY = staff.ReadStaffAuthority()


def prepare_submission_delivery(saved: review_submissions.SavedSubmission, words: str,
                                status: change_page.DeliveryStatus, *, evidence_page: str = "",
                                members: staff.StaffSnapshot | staff.ReadStaffAuthority = _READ_STAFF_AUTHORITY
                                ) -> change_page.MessageDelivery | change_page.NoMessageDelivery:
    """Claim the first message for a published native request."""
    with staff.supplied_snapshot(members) if isinstance(members, staff.StaffSnapshot) else nullcontext():
        return _claim_submission_delivery(saved.address, status, words=words, evidence_page=evidence_page)


def _ensure_list_filing(saved: review_submissions.SavedSubmission) -> None:
    """Finish a staged List row before claiming its notice."""
    route = review_submissions.list_filing_route(saved)
    if isinstance(route, review_submissions.NoListFiling):
        return
    if isinstance(route, review_submissions.ListFilingUnavailable):
        review_submissions.skip_list_filing(saved, route.reason)
        return
    key = review_items.submission_review_key(saved)
    evidence = REVIEW_PATH + "?show=all#" + urlencode({"show": "open", "jump": key})
    words = saved.words
    try:
        _ = the_list.add_review_item(route["keeper"], review_submissions.submission_title(words), owner=route["owner"],
                                     producer=route["producer"], unit=route["unit"], words=words, evidence=evidence,
                                     review_key=key, source=route["source"],
                                     reservation=review_submissions.list_filing_reservation(saved))
    except the_list.RetiredListProducer as error:
        review_submissions.skip_list_filing(saved, str(error))


def _claim_submission_delivery(address: review_submissions.SubmissionAddress, status: change_page.DeliveryStatus, *,
                               words: str = "", evidence_page: str = ""
                               ) -> change_page.MessageDelivery | change_page.NoMessageDelivery:
    # Finish the List row under the recipient locks before taking the delivery lock.
    with review_submissions.submission_for_delivery(address) as filing:
        if isinstance(filing, review_submissions.NoNativeSubmission):
            return change_page.NoMessageDelivery()
        _ensure_list_filing(filing.saved())
        assignment = filing.details.assignment
    assignee = ""
    if isinstance(assignment, new_item_dialog.ChosenAssignee):
        name = review_items.name(assignment.id, staff.snapshot())
        assignee = f"\n{name} is the assignee."

    with status.locked(), review_submissions.submission_for_delivery(address) as located:
        if isinstance(located, review_submissions.NoNativeSubmission):
            return change_page.NoMessageDelivery()
        saved = located.saved()
        if not saved.awaiting_reply:
            review_submissions.clear_initial_delivery(address)
            return change_page.NoMessageDelivery()
        row, path = saved.request_id, saved.original_path
        if path == saved.path and saved.owner != saved.recipient_id:
            path = status.previous_submission_source(row, path)
        placeholder = change_page.ReviewPage(path, path.parent, (row,), '')
        stored = status.metadata_page(placeholder)
        slots = status.slots(placeholder, row)
        intent = slots.verdict
        if isinstance(stored, change_page.ReviewMessagePage) and stored.saved.get('submission_request') == row:
            if stored.saved.get('submission_delivered'):
                review_submissions.clear_initial_delivery(address)
                return change_page.NoMessageDelivery()
        if isinstance(intent, change_page.DeliveryIntent) and status.owns_live_claim(placeholder, row, intent.attempt):
            raise SubmissionDeliveryPending("This item is still sending.")
        saved = review_submissions.renew_legacy_reply(located)
        if not saved.awaiting_reply:
            review_submissions.clear_initial_delivery(address)
            return change_page.NoMessageDelivery()

        command: change_page.ScopedReplyCommand = {'key': saved.key, 'token_sha256': saved.details.reply_token_sha256}
        if (isinstance(stored, change_page.ReviewMessagePage) and stored.saved.get('submission_request') == row
                and stored.saved.get('scoped_reply') == command):
            page = stored
        else:
            evidence = ""
            if evidence_page:
                key = review_items.submission_review_key(saved)
                evidence = "\n\nEvidence in Review\n" + evidence_page + '#' + urlencode({'item': key})
            notice = saved.details.notice
            label = saved.kind.value
            source = " from Review"
            if isinstance(notice, new_item_dialog.SubmissionNotice):
                if not isinstance(notice.kind, new_item_dialog.SubmissionKind):
                    label = notice.kind.capitalize()
                if notice.from_command:
                    source = ""
            message = (f'New {label} {saved.id}{source}.{assignee}\n\n{words or saved.words}{evidence}\n\n'
                       + change_page.reply_command(saved.key))
            record: change_page.StoredReviewMessage = {
                'source': str(path), 'row': row, 'saved': True, 'owner': saved.owner,
                'message': message, 'submission_request': row, 'scoped_reply': command,
            }
            page = change_page.ReviewMessagePage(path, path.parent, (row,), '', record)
            # A new proof changes the message. Give it a new delivery key.
            intent = change_page.NoDelivery()
        if not isinstance(intent, change_page.DeliveryIntent):
            atomic_file.sync_folder(saved.path.parent)
            recipient = staff.StaffId(page.saved['owner'])
            intent = change_page.DeliveryIntent(secrets.token_hex(), 'verdict', recipients=(recipient,))
        delivery = change_page.MessageDelivery(page, row, intent.recipients, page.saved['message'], intent.attempt,
                                               (staff.StaffId(page.saved['owner']),))
        _ = change_page.outbound_review_message(delivery)
        if slots.verdict != intent:
            status.write_slots(page, row, slots.updated('verdict', intent))
        status.request_attempts[intent.attempt] = change_page.AttemptOwnership(status.path(page, row), 'claimed')
        review_submissions.clear_initial_delivery(address)
    return delivery


def recover_submission_deliveries(status: change_page.DeliveryStatus, review_path: str) -> None:
    """Claim the first messages that were interrupted before the claim saved."""
    for pending in review_submissions.pending_initial_deliveries(status.root):
        try:
            page = pending.evidence_origin + review_path if pending.evidence_origin else ""
            delivery = _claim_submission_delivery(pending.address, status, evidence_page=page)
            try:
                change_page.deliver(delivery, status)
            finally:
                if isinstance(delivery, change_page.MessageDelivery):
                    status.release_owned_claims((delivery.attempt,))
        except (OSError, ValueError, RuntimeError, staff.StaffRefused) as error:
            print(f"Review first message {pending.address.request_id}: {error}", file=sys.stderr)


def save_delivery(page: change_page.ReviewMessagePage | change_page.UserQuestionPage, owner: staff.StaffId,
                  save: Callable[[], object], status: change_page.DeliveryStatus,
                  question: change_page.NoQuestion | change_page.OpenQuestion) -> change_page.MessageDelivery:
    row = page.rows[0]
    with status.locked():
        previous_page = status.metadata_page(change_page.ReviewPage(page.review_list, page.production, page.rows, page.token))
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
                _ = investigation.review_action(record.owner, record.id, action, words, new_reply_proof=scoped.proof, check=verify_revision)
            else:
                investigation.change_waiting(record, action, words, check=verify_revision)
            with review_items.after_store_write(item.key):
                record_superseded(item, action)
        elif isinstance(scoped, ScopedWait):
            _ = investigation.review_action(record.owner, record.id, action, words, new_reply_proof=scoped.proof, check=verify_revision)
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
    delivery = send(item,path,record.id,message,save,status,recipient=item.owner,
                    question_text=words if action == 'question' else '', scoped=scoped)
    return delivery,('question sent' if action == 'question' else 'feedback sent')


def approval_assignees(members: staff.StaffSnapshot) -> tuple[staff.StaffRow, ...]:
    """List the current showrunners and unit directors by name."""
    return tuple(sorted(staff.active(members, 'showrunner') + staff.active(members, 'unit'),
                        key=lambda member: (member.name.casefold(), member.id)))


def decision_action(item: review_items.ReviewItem, action: str, words: str,
                    status: change_page.DeliveryStatus, *, assignee: str = '',
                    verify_revision: Callable[[], object] | None = None
                    ) -> tuple[change_page.MessageDelivery | change_page.NoMessageDelivery, str]:
    """Apply Approve, Decline, Send feedback or Ask question to a proposal or an efficiency."""
    source = item.source
    if action in ('feedback','question') and not words.strip():
        raise question_record.AnswerRefused('enter your words')
    waiting = item.state == review_items.ReviewState.WAITING
    recipient = item.owner
    approve_proposal_for_unit = False
    logged = {'feedback':'feedback sent','question':'question','approve':'Approve','decline':'Decline'}[action]
    message = message_text(item,action,words)
    if action == 'approve':
        members = staff.snapshot()
        if assignee:
            assigned = next((member for member in approval_assignees(members) if member.id == assignee), None)
            if assigned is None:
                raise question_record.AnswerRefused('Choose a current showrunner or unit director.')
            approve_proposal_for_unit = isinstance(source, review_items.ProposalSource) and assigned.kind == 'unit'
            recipient = assigned.id
            answer = logged = f'approved, assigned to {assigned.name}'
            if not approve_proposal_for_unit:
                message += '\nThe user assigned this item to you.'
        else:
            answer = 'approved, sent to '+review_items.name(item.owner, members)
    else:
        answer = {'decline':'declined','feedback':'feedback sent','question':'question sent'}[action]
    logged += ': '+words if words else ''
    mark: MarkChange = ReviewMark.WAITING if action == 'question' else ReviewMark.DONE
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
        delivery = send(item,efficiency_reviews.path(source.path),identity,message,save_efficiency,status,recipient=recipient,
                        question_text=words if action == 'question' else '')
    else:
        assert isinstance(source, review_items.ProposalSource)
        issue = source.record.path
        origin = proposal_issue.review_submission(source.record) if action == 'question' else new_item_dialog.NoReviewSubmission()
        scoped = (ScopedWait(review_submissions.submission_key(new_item_dialog.SubmissionKind.PROPOSAL,
                             staff.StaffId(source.record.value('proposed_by')), source.record.value('proposal_id'), origin.request_id))
                  if isinstance(origin, new_item_dialog.ReviewSubmission) else NO_SCOPED_WAIT)
        if approve_proposal_for_unit:
            message += '\nThe user approved the proposal and assigned it to you. The approval is recorded.'
        elif action == 'approve':
            message += ('\nApprove accepts the proposed ranking; the words above can change a value. Record it:\n'
                        +PY+'production/proposal_issue.py approve '+shlex.quote(str(issue))+' --by user --unit <staff id>')
        elif action == 'decline':
            message += '\nThe page recorded the decline. Close the issue with /close_issue.'

        def save() -> None:
            day = datetime.now(user_zone()).date()
            if approve_proposal_for_unit:
                proposal_issue.approve_from_review(issue, recipient, day, events, check=verify_revision)
            elif action == 'decline':
                _ = proposal_issue.edit(issue, {'style_answer':'declined', 'style_answer_words':words},
                                        day, check=verify_revision)
                with review_items.after_store_write(item.key):
                    _ = proposal_issue.record_review(issue, events, NoMark(), day)
            else:
                _ = proposal_issue.record_review(issue, events, mark, day, check=verify_revision,
                                                 waiting_why=words if action == 'question' else '',
                                                 new_reply_proof=scoped.proof if isinstance(scoped, ScopedWait)
                                                 else reply_tokens.MINT_REPLY_PROOF)
            if waiting:
                with review_items.after_store_write(item.key):
                    log_superseded(item, events[0])
        delivery = send(item,issue,issue.stem,message,save,status,recipient=recipient,
                        question_text=words if action == 'question' else '', scoped=scoped)
    return delivery,answer

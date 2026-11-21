# Review answer lifecycle

## 1. Records

| HEAD record and key | Writer and saved evidence |
| --- | --- |
| Question Markdown, by owner and `Q` ID or New Item request | `_ask_recipient` and `save_question_record` file or update a wait. New Item first stores `followup` and an origin token. `reply_record` and `reply_to_submission` save an unkeyed Answer and retain the completed pair in `followups`. Closed records can retain the log. `scripts/production/update_registration.py:1830`, `scripts/production/update_registration.py:2248`, `scripts/production/update_registration.py:2302`, `scripts/review_links/change_browser.py:730`. |
| Bug and Investigation Markdown, by store and `I` ID | `file` starts Open with an empty answer and adds Sent for New Item. `_answer` saves Answer Ready, body text, time, and an unkeyed Answer. `review_action` can reopen and rotate the token. `mark_read` and `change_waiting` can leave Read with stale or empty answer text. `scripts/production/investigation.py:293`, `scripts/production/investigation.py:381`, `scripts/production/investigation.py:387`, `scripts/production/investigation.py:404`, `scripts/production/investigation.py:445`. |
| Proposal Markdown, by store and `P` ID | `file_review_proposal` creates an open proposal with Waiting and an origin token. `record_review` saves marks and ordered events. A scoped Ask can rotate its token. `reply_to_submission` and `decision_store.answer` append unkeyed Answers. `reopen` removes close fields. `_move_one` clears Waiting when it changes the stage. `scripts/production/proposal_issue.py:171`, `scripts/production/proposal_issue.py:444`, `scripts/production/proposal_issue.py:486`, `scripts/review_links/decision_store.py:61`, `scripts/production/proposal_issue.py:572`, `scripts/production/proposal_issue.py:816`. |
| Efficiency ledger and `.review.json`, by ledger and row | `efficiency_reviews.record` saves the Review mark, waiting reason, and log. `decision_store.answer` saves an unkeyed Answer. `candidates.record_outcome` writes ledger dispositions. A terminal ledger disposition makes the item Done even when the Review mark is Waiting. `scripts/review_links/efficiency_reviews.py:97`, `scripts/review_links/decision_store.py:61`, `scripts/nightly_review/candidates.py:320`, `scripts/nightly_review/candidates.py:342`, `scripts/review_links/review_items.py:1062`. |
| Change Markdown, by resolved list and row | `record_review_log` saves Ask, feedback, Answer, and action entries. `answer_item` saves delivery first. The `--list` path saves its answer only in the slot, which a later Ask can replace. Single and batch verdicts can log a failed validation while the row stays open. `scripts/production/change_review.py:1995`, `scripts/production/change_review.py:2266`, `scripts/production/change_review.py:2850`, `scripts/review_links/change_page.py:1815`, `scripts/review_links/change_page.py:4779`, `scripts/review_links/change_page.py:4833`. |
| New Item draft markers and receipts, by recipient, page, or request ID | `_locked_draft_request` can save `.indexed`, recipient markers, and page markers. Binding markers store a request ID. `submit` saves `pending.json` with the payload hash and then the native location. `_complete_receipt` promotes or repairs `receipt.json`. A move changes its location. `scripts/review_links/review_submissions.py:466`, `scripts/review_links/review_submissions.py:539`, `scripts/review_links/review_submissions.py:613`, `scripts/review_links/review_submissions.py:765`, `scripts/review_links/review_submissions.py:1080`, `scripts/production/tidy_wrapped.py:116`. |
| Record number, `record-ids/<kind>.water` | Outside the module. `LockedRecordNumbers.write` saves the highest number under the allocator lock. A crash can skip a number. `scripts/production/record_store.py:419`, `scripts/production/record_store.py:571`. |
| Folder registry, `review-folders/<owner>/<kind>.json` | Outside the module. `LockedRecordNumbers.write` saves this list for allocation under the allocator lock. `scripts/production/record_store.py:57`, `scripts/production/record_store.py:571`. |
| New Item evidence and List link, by request and List keeper or item | `_save_images` publishes the images before filing. `save_bug_page_state` saves page evidence. `attach_page_screenshot` can update the native Question or Investigation after delivery. `add_submission` finds or adds a List row and links its Review key. `scripts/review_links/review_submissions.py:211`, `scripts/review_links/review_submissions.py:1148`, `scripts/review_links/review_submissions.py:1175`, `scripts/review_links/review_submissions.py:1186`, `scripts/review_links/serve.py:1172`, `scripts/review_links/list_page.py:113`. |
| `.review-link-delivery-*`, by source path and row | `prepare_submission_delivery` uses the original path and request ID with no question slot. `save_native_question` saves a slot, verdict intent, and progress. Change can save `OpenFeedback` or `CompletedFeedback`. HEAD reads flat verdicts and validations, intents or questions without progress, and followups with or without slots. A slot can be open, pending, or answered. Terminal progress or `submission_delivered` can survive without an intent. `scripts/production/change_routing.py:325`, `scripts/review_links/review_actions.py:270`, `scripts/review_links/review_actions.py:360`, `scripts/review_links/change_page.py:1710`, `scripts/review_links/change_page.py:1774`, `scripts/review_links/change_page.py:4033`–`scripts/review_links/change_page.py:4161`. |
| Unsaved verdict intent, by delivery row | `save_delivery` reserves a verdict before the source save. It can restore the previous slots on failure or mark the intent saved on success. `scripts/review_links/review_actions.py:327`–`scripts/review_links/review_actions.py:354`. |
| `change_delivery` entries, by intent kind | `write_change_progress` saves progress, message, and per-recipient routes. A question can include `feedback_source` with path, row, and alert recipients. `scripts/review_links/change_page.py:3412`–`scripts/review_links/change_page.py:3426`. |
| `user_answer`, by delivery row | `DeliveryStatus.write_slots` stores the Question source, action, answer, message, and save state. A missing kind means answer in an older record. `scripts/review_links/change_page.py:244`, `scripts/review_links/change_page.py:1682`. |
| `review_message`, by delivery row | `DeliveryStatus.write_slots` stores the record source, owner, message, and save state. An older record can omit the row. `scripts/review_links/change_page.py:170`, `scripts/review_links/change_page.py:1687`. |
| Schema 3 `question_attempt` envelope, by delivery path | `publish_question_attempt` saves the message, message ID, source, recipient, revision, progress, owner, and receipt. `scripts/review_links/change_page.py:666`. |
| `.review-link-saved-*`, by delivery row and attempt | `write_save_receipt` saves the exact attempt as legacy proof of a saved action. Question receipt names include the attempt hash. `scripts/review_links/change_page.py:1446`, `scripts/review_links/change_page.py:1461`. |
| `.review-link-closeout-alert-*.json`, by delivery row | `write_closeout` saves failures, pending alerts, alert history, and dispatch markers. Old alerts can contain only the recipient and message. `scripts/review_links/change_page.py:183`, `scripts/review_links/change_page.py:1314`, `scripts/review_links/change_page.py:1491`. |
| `.review-link-question-recovery-*`, by delivery path | Legacy input. `old_recovery` reads the attempt, question ID, error, and optional deadline. `scripts/review_links/change_page.py:3743`. |
| Message transport | Outside the module. `send.py` keeps its existing rules for reservations, start, publication, receipts, and recovery. |
| Change `.review-link-scratch-*`, by list and row | `PendingVerdictCleanup.save` saves the expected verdict and optional feature, wait, and Git data before the verdict write. Recovery checks the verdict before closeout. `scripts/production/change_review.py:2143`, `scripts/review_links/change_page.py:2614`, `scripts/review_links/change_page.py:3614`. |
| Validation queue, by queue and row | `change_page.submit` calls `next_item.move_to_end` before the validation log write. `urgency.move_to_end` saves the end time and clears the pin if it names that row. `scripts/review_links/change_page.py:2548`, `scripts/production/next_item.py:168`, `scripts/lib/urgency.py:395`. |
| Later deadline and Change handoff time | `review_later.save` writes the deadline before the native Later log. `clear_change_later` removes it before delivery replacement. `DeliveryStatus.write_slots` saves `handed_at` with the new slots. `scripts/review_links/review_later.py:62`, `scripts/production/change_review.py:2204`, `scripts/review_links/change_page.py:1715`. |
| Producer Question audit files, by owner and question | `record_answer` and `record_question_history` save answers, filings, and withdrawals. `submit_question` also writes the production log. `scripts/production/update_registration.py:652`, `scripts/production/update_registration.py:2373`, `scripts/production/update_registration.py:2389`, `scripts/review_links/change_browser.py:768`. |
| List and Images question records, by store, item, and question ID | List stores request receipts and closed question entries. `_ListQuestionStore.set_answer` saves the Answer, appends `QuestionAnswered`, and sets `unread_since` in one write. `ImageQuestionStore.set_answer` saves the Answer in `.review-link-image.json` and removes `feedback_at`. `finish_feedback` can restore `feedback_at` after confirmed feedback for the current answers. List `save_feedback` and `deliver_feedback` save requests and results. `scripts/production/the_list.py:1584`, `scripts/production/the_list.py:1793`, `scripts/production/the_list.py:1797`, `scripts/production/the_list.py:1807`, `scripts/production/the_list.py:1860`, `scripts/production/the_list.py:1880`, `scripts/review_links/image_store.py:109`, `scripts/review_links/image_store.py:120`, `scripts/review_links/images.py:551`. |
| List item state, by keeper and item | `reopen` resets the closed state. `scripts/production/the_list.py:1601`. `mark_read` clears `unread_since`. `scripts/production/the_list.py:1642`. `unpark_for_delivered_change` saves Delivered and sets `unread_since`. `scripts/production/the_list.py:1552`. |
| Images feedback request, `.review-link-feedback-<key>.pending` | `ImageFeedback.save` writes the owner, registration time, answered IDs, action, and text. `scripts/review_links/images.py:394`, `scripts/review_links/images.py:600`. |
| Images feedback snapshot, `.review-link-feedback-<key>` | HEAD reads this older format for the previously answered IDs. `scripts/review_links/images.py:416`. |
| Images feedback receipt, `.review-link-feedback-<key>.json` | `write_feedback_progress` saves send progress for the feedback key. `scripts/review_links/change_page.py:2888`, `scripts/review_links/images.py:580`. |
| List feedback receipt, `.review-link-feedback-<key>.json` beside the item | `list_page.deliver` calls `deliver_page_feedback` to save the receipt. `the_list.deliver_feedback` records the result on the List item. `scripts/review_links/list_page.py:909`, `scripts/review_links/list_page.py:914`, `scripts/review_links/list_page.py:931`. |

## 2. Events

Keep the private exchange log outside the delivery directory. The log indexes Asks, closes, and filing fences. Resolve moved records from filing or move evidence.

| Kind | Where to write the Ask | Where to write the closing entry |
| --- | --- | --- |
| New Item Question, Proposal, Bug, and Investigation | In the private log for the bound request ID (§3). Use it for later scoped Asks. Save images and page data with the Ask. A transient screenshot can remain absent. | In the same request log. Apply the close at the current native location and the saved delivery address. |
| New Item from the List | Save the original List keeper in the request Ask. Reserve the row in the opening journal entry. | Use the request log. For a closed row, skip the link and continue delivery. `scripts/review_links/list_page.py:118`, `scripts/review_links/serve.py:1147`. |
| Ordinary Question, on a Review follow-up | Start the private log with the Review follow-up under the native question key. Ordinary filing keeps the HEAD path. `scripts/production/update_registration.py:1887`–`scripts/production/update_registration.py:1909`. | In the same private log. |
| Ordinary Bug, Investigation, Proposal, and Efficiency | In the source log for the native store and item or ledger row. | In the same source log. |
| Change question, feedback that expects a reply, and validation | In the source log for the Change list and row. Save the kind for the delivery slot. | In the same Change source log. In both verdict paths, close the validation and each Ask that the failure ends. |
| List and Images questions | Apply HEAD receipt and open question selection before opening an Ask. Continue the selected question in its existing exchange. Open a private Ask and reserve its question ID only for a fresh question. `scripts/production/the_list.py:1834`, `scripts/production/the_list.py:1847`, `scripts/review_links/page_question.py:40`. | In the private log, for the same question ID. Keep the close if the item or image content is removed. |

| Event | Durable order and restart action |
| --- | --- |
| Draft creation or recipient change | Keep the unbound draft ID. Check for a legacy filing before binding a draft retry to a different recipient (§3). After opening, use the Ask's saved reply address. |
| First delivery and the first scoped follow-up | Open an exchange through the write module. |
| Answer, verdict, withdrawal, failed validation, cancel, read, or archive that ends the exchange | Close the selected exchange through the write module. Include its answer notice when the action sends one (§4). |
| Write failure after a visible answer or close | Replay the unfinished journal entry (§4). |
| Reopen or replace a wait | Retire the previous wait in the operation that opens the new Ask. Preserve prior answers. |
| Archive relocation or ownership transfer | Preserve the exchange ID and saved delivery address. Transfer sends no extra notice. |
| Showrunner rename | Keep the staff ID and exchange address. Resolve only the current display name. |
| Restart and live retry | Under the module lock, enumerate private Asks and pending answer notices before legacy scans. Include Asks with no delivery record. |
| HEAD records with unkeyed answers | Use the legacy behavior in §3. |

## 3. Exchange identity and scope

An exchange is new only when its opening Ask is in the private log. Reconcile only private exchanges there. Keep HEAD behavior and owner visibility for legacy exchanges. An unkeyed legacy Answer cannot close a private Ask.

Write `exchange_id` with each Review delivery entry. Index List and Images Asks by their existing store, item, and question IDs. Omit the duplicate native field. Other native entries carry `exchange_id`. Select the current delivery entry before checking its ID or tuple. Preserve entry order on mixed rows.

Mint and store the exchange ID at opening for the other stores. A selected legacy question uses the HEAD path, including any new `PageRequest` receipt. If no native question matches, check private reservations before allocation. Match a List retry by `PageRequest` or the same open words. Match an Images retry by the same open words.

Save the original delivery address and transport key in the Ask. The address holds the original delivery filename and row. For New Item, use the filing path and bound request ID. Keep the exchange's delivery effects at its saved address. Match the exchange and attempt before changing a slot or alert there. Resolve the current native record separately. Close and replay use the saved address and key after a move. A later scoped Ask saves its own address and key. `scripts/review_links/review_actions.py:274`, `scripts/review_links/review_submissions.py:1260`, `scripts/production/change_routing.py:325`, `scripts/review_links/change_page.py:2839`.

Index the issued alias and token with the Ask at opening. Resolve the reply through this index before the current-item lookup. Apply the answer at the current native location and the saved delivery address. `scripts/review_links/review_actions.py:255`, `scripts/review_links/review_items.py:847`, `scripts/review_links/change_page.py:4732`.

For a fresh bound request, accept only `r1`. For its retry, look up the Ask by the supplied ID. Use that ID and recipient for a fresh Ask. Reject bound `r3` and `r5` for filing as HEAD does. Keep the `r5` page binding for draft filings and scoped replies. `scripts/review_links/new_item_dialog.py:261`, `scripts/review_links/review_submissions.py:370`, `scripts/review_links/review_submissions.py:1017`.

Use the draft key as the private Ask's lookup key. Compare the fingerprint under that key. The fingerprint includes the key, kind, normalized words, and image extensions and byte hashes. A match reuses the Ask and recipient. Changed content starts a new Ask. The earlier Ask completes independently. Derive the bound ID for a new draft as HEAD does. `scripts/review_links/new_item_dialog.py:879`, `scripts/review_links/review_submissions.py:370`, `scripts/review_links/review_submissions.py:1026`.

If the lookup finds no Ask, check for a legacy filing before opening. Check the draft's previous bindings before choosing a different recipient. A matching HEAD `pending.json` counts as a legacy filing even if it holds only the payload hash. Continue HEAD recovery at the saved binding. Defer if the lookup cannot establish absence. `scripts/review_links/review_submissions.py:470`, `scripts/review_links/review_submissions.py:584`, `scripts/review_links/review_submissions.py:1080`.

For a new filing, select the recipient and allocate the native ID before the opening journal entry. Save the reply address and token in the Ask. Save the request fingerprint. Validate `submission/<kind>/<recipient>/<item>/<request>` with `read_submission_key`. Filing and replay use the allocated ID and saved address after a recipient or owner change. `scripts/production/record_store.py:561`, `scripts/review_links/new_item_dialog.py:278`.

List allocation honors the saved row reservation. Verify the request before reusing the row.

## 4. Rules

1. **Use one write module and lock.** `scripts/production/review_writes.py` owns `open`, `close`, and one lock per machine. On each acquisition, replay every pending entry before any operation or eligibility read. Require the lock for Review eligibility checks, including legacy recovery.

   The module owns these files.

   - Native records, including Efficiency dispositions and `.review.json`, and the private exchange log.
   - Filing, location, move, and cleanup records. Include New Item evidence and native screenshot attachments.
   - The validation queue, Later deadlines, and `handed_at`.
   - Review delivery slots, progress, receipts, and closeout.
   - Images `.pending` requests and send receipts, the legacy Images feedback snapshot, and List `.review-link-feedback-<key>.json` receipts.

   In the code phase, check this boundary with a structural test. Include replacements, removals, and lower-level writer calls.

2. **Write the journal first.** Write one durable entry before changing any target file. Record the complete replacement or removal of each target. For a Change batch, include all edits and verdicts for each affected list. On replay, sync the journal entry and its folder first. Apply target changes only after both syncs succeed. Apply the entry idempotently. Durably remove it after all target changes are durable. Use `atomic_file` for durable replacements. `scripts/lib/atomic_file.py:46`, `scripts/lib/atomic_file.py:114`, `scripts/production/change_review.py:2911`.

   Journal native and delivery changes with each Ask or close. Include an Efficiency disposition that ends the wait. Use the journal as proof of a private save. Use `.review-link-saved-*` only for legacy recovery. A standalone Later action uses only the write journal.

   If a close needs an answer notice, save the notice and its own attempt in the close entry. Journal the close and the notice send together. The close cancels every pending Ask send. `scripts/review_links/change_browser.py:756`, `scripts/review_links/change_browser.py:819`, `scripts/review_links/change_page.py:4640`.

3. **Use the saved exchange state.** When an Ask supersedes a legacy New Item, journal a fence with the Ask. Key the fence by the original filing request ID. Keep it after the Ask closes. Legacy senders and recovery skip a fenced filing.

   Journal feedback receipts and matching source completion together when the result is known. Use an existing terminal receipt to complete its source without another send. Check the Images answered IDs before setting `feedback_at`.

4. **Hand off under the lock.** Check the saved attempt for each send. Require an open exchange for an Ask send. Recover an answer notice by its own saved attempt in the close entry. Hand the attempt to `send.py` in one call with its stable transport key. Release the lock before waiting. Apply the result under the lock to the matching exchange and attempt.

   A close stops Ask attempts before handoff to `send.py`. As in HEAD, a started Ask send can deliver once more just after close.

## 5. Cases

| Carried finding | Rule and closure |
| --- | --- |
| F001 | 2, 3. Replay a missing projection from the journal. |
| F002 | Legacy keeps HEAD behavior (§3). New first delivery uses the saved Ask and alias. |
| F003 | 2. Replay the same close after a write error. |
| F006 | 2. Save normalized words before delivery. |
| F007 | §3. Resolve an issued reply alias before the current-item lookup. |
| F008 | Legacy keeps HEAD behavior (§3). Preserve terminal Review receipts for private exchanges. |
| F009 | 2. An older Answer cannot confirm a new save. |
| F011 | 3. Use question delivery for new Review asks. |
| F012 | 3, 4. Check the original Review receipt before handoff. |
| F013 | 1, 4. Replay an unfinished transfer before the handoff check. |
| F014 | Legacy keeps HEAD behavior (§3). |
| F015 | 2, 3. Read closes a named Ask. Stale text cannot become its Answer. |
| F016 | 3. The card reads the same binding and delivery stamps as recovery. |
| F019 | 2. A failure before journal publication leaves the exchange open. |
| F018 | 1, 2. Confirm List and Images saves by item and question ID under the module lock. |

## 6. Write module

Start from main after askrace and askgaps (`34bb302371`). Worktree HEAD `d7c6881785` includes both. Askgaps supplies F004, F005, F010, and F017.

Deploy the updated code on natedev and the Mac. Restart both Review servers before any new exchange opens. CLI writers load the new code on each run. HEAD writes before this boundary produce legacy records.

Keep the current browser and CLI commands. Route their writes and eligibility reads through the module. Preserve exchange IDs on native rewrites, including both screenshot attachment paths. Use existing IDs in `question_record.read` and `stored` for List and Images. Use the table as a guide. The structural test guards the writer boundary.

| Area | HEAD call sites |
| --- | --- |
| Review actions | `review_actions.ask_user_question`, `question_action`, `sent_question_action`, `investigation_action`, `decision_action`, `send`, `save_delivery`, `save_native_question`, `record_superseded`, and `clear_wait` |
| New Item | `review_submissions.submit`, `_locked_draft_request`, `_save_images`, `_file`, `_file_question_for_remote_owner`, `_complete_receipt`, `save_bug_page_state`, `serve.new_item_post`, `finish_bug_evidence`, `list_page.add_submission`, and `review_actions.prepare_submission_delivery` |
| Screenshot attachments | `review_submissions.attach_page_screenshot` calls `update_registration.write_question_record` for Question and `_write_investigation_submission` for Bug and Investigation. `scripts/review_links/review_submissions.py:1148`, `scripts/review_links/review_submissions.py:1158`, `scripts/review_links/review_submissions.py:1175`. |
| Question forms and CLI | `change_browser.submit_question`, `save_outcome`, `save_question_record`, `review_page.submit_snapshot`, `submit_response`, `review_submissions.answer`, and `update_registration._ask_recipient`, `write_question_record`, `review_sent_question`, `reply_to_submission`, `reply_record`, `write_selected_item`, `act_on_question`, `close_questions`, and `store_questions` (`scripts/production/update_registration.py:1462`) |
| Question audits | `update_registration.record_answer`, `record_question_history`, and `append_log` |
| Investigation and Proposal | `investigation.file`, `_answer`, `mark_read`, `review_action`, `change_waiting`, and `record_review_log`. `proposal_issue.file_review_proposal`, `reply_to_submission`, `record_review`, `edit`, `_approve`, `reopen`, and `_move_one`. |
| Efficiency | `candidates.record`, `outcome`, and `record_outcome`. `decision_store.answer`, `efficiency_reviews.record`, and `review_actions.resume_efficiency`. |
| List and Images questions | `the_list.ask_question`, `_ListQuestionStore.add`, `save_request`, `set_answer`, and `the_list.close`. `images.ask_question`, `answer_question`, and `delete`. `ImageQuestionStore.add` and `set_answer`. `page_question.ask` and `save_attempt`. |
| List item state | `the_list.reopen`, `mark_read`, and `unpark_for_delivered_change` |
| List and Images feedback | `the_list.save_feedback` and `deliver_feedback`. `list_page.deliver` and `recover_pending`. `ImageFeedback.save`, `images.advance_feedback`, `finish_feedback`, and `recover_pending`. `change_page.deliver_page_feedback` and `write_feedback_progress`. |
| Change handoff and queue | `change_browser.submit_review_change`, `change_page.submit`, `DeliveryStatus.begin_locked`, `ask_locked`, and `write_slots`, `change_review.record_review_log`, `next_item.move_to_end` and `show`, `change_review._run` for `show`, and `urgency.move_to_end`, `top`, `top_across`, and `pin` on the Change queue |
| Later | `review_actions.later` and `record_action`, `review_page.submit` and `submit_snapshot`, `review_later.save`, and `change_review.clear_change_later` |
| Change close, batch, and cleanup | `change_review.checked_review_write`, `try_review_write`, `save_verdict`, `record_verdict`, `withdraw_row`, and `batch`, `DeliveryStatus.answer_question` and `supersede_wait`, `answer_item`, `clear_saved_verdict_wait`, and `recover_verdict_cleanups` |
| Delivery | `change_review.review_delivery_lock`, `DeliveryStatus.locked`, `native_action`, `mark_review_saved`, `clear`, `clear_locked`, `finish_intent_locked`, `write_closeout`, `delivery_state.complete_review_reply`, `publish_question_attempt`, `advance_question_attempt`, `record_page_question_answer`, `deliver_page_question`, `_lock_open_question_source`, `_claim_recovered_page_question`, `finish_review_question`, `finish_change_message`, `write_review_progress`, and `write_change_progress` |
| Save receipt write | `DeliveryStatus.write_save_receipt` (`scripts/review_links/change_page.py:1461`) |
| Save receipt removal | `DeliveryStatus.remove_save_receipt` (`scripts/review_links/change_page.py:1789`) |
| Change row kind | `change_review.set_row_kind` (`scripts/production/change_review.py:2003`) |
| List item creation | `the_list.add` (`scripts/production/the_list.py:1221`) |
| List producer move | `the_list.move_open_producer` (`scripts/production/the_list.py:1459`) |
| Images registration | `images.register` (`scripts/review_links/images.py:150`) |
| Recovery and moves | `recover_pending`, `recover_deferred_questions`, `recover_change_question_sources`, `review_actions.recover_questions`, `page_question.recover_source`, `migrate_delivery_file`, `expire_saved_questions`, `recover_change_messages`, `recover_review_messages`, `recover_question_answers`, and `tidy_wrapped.ReviewRecordMove.apply` |

Drop the patch's reply retry directory, fixed native paths, owner equality check, last-answer fallback, legacy binding, ambient key context, and feedback changes that askgaps already supplies.

## 7. Design review findings

| Finding | Disposition |
| --- | --- |
| 9.1, 9.3, 10.1, 10.2, 11.1, 11.2, 11.3, and the transport parts of 9.4, 10.4, 11.5, and 11.6 | outside: send.py owns the transport (§4). |
| 9.2. Shared-file replay can reverse the lock order | §4. Replay uses one module lock. |
| 9.4. Allocator state | §1. The allocator owns record numbers and the folder registry. |
| 10.3. A bound request lacks an opening identity | §3. Use the supplied ID for an accepted bound `r1` filing. |
| 10.5. Native writers are missing | §§1 and 6. Include the Proposal and List writers. |
| 11.4. A partial HEAD filing can open a private Ask | §3. Continue HEAD recovery for a matching `pending.json` before binding a new recipient. |
| 11.5. List citations | §1. Cite each writer at its definition. |
| 11.6. Native Question writer | §6. Include `store_questions`. |
| 12.1. Close can suppress an answer notice | §4. Journal the notice with the close and recover it by its own attempt. |
| 12.2. An issued reply can fail after a move | §3. Resolve its alias before the current-item lookup. |
| 12.3. Replay can apply an unconfirmed entry | §4. Sync the journal entry and its folder before target changes. |
| 12.4. Review delivery shapes are missing | §1. List the six shapes and identify the legacy recovery input. |
| 12.5. The call-site table omits writers | §6. Include the named writers. The structural test guards the boundary. |
| 12.6. The bound filing rule accepts too many IDs | §3. Only bound `r1` can file. Keep the `r5` page binding for drafts and replies. |

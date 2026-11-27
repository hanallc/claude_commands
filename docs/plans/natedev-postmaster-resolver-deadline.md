# Fast recipient lookup in send.py

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** A cold `send.py` recipient lookup returns in a short time, so a caller that sends before an action (the build-hold enforcer before TERM) does not wait about 20 s per recipient.

> **As-built disposition: amend** — `docs/as-built/session-roster.md`

## Delegation Context

- **Project:** `~/.claude` scripts — `send.py` recipient resolution.
- **Project started:** 2026-11-21T13:28:35.019+00:00
- **Stack:** Python 3 (basedpyright strict, unittest).
- **Layout:** `scripts/message/`, `config/`, `docs/as-built/`.
- **Key files:**
  - `scripts/message/send.py` — `resolve_recipient()` and the lookups it calls (`_staff_recipient_match`, `sessions.live_sessions`, `matching_recipients`, `resolve_staff_recipient`).
  - `scripts/message/roster.py`, `scripts/message/sessions.py` — the roster and live session reads.
  - `config/send.conf` — send.py time budgets (`delivery_timeout`, `check_timeout`, and others).
- **Test lanes:** `scripts/message/test_send.py`, `scripts/message/test_roster.py`, `scripts/message/test_send_delivery.py`, and the `test_*.py` beside any other changed file.
- **Build:** none.
- **Test:** `~/.claude/scripts/lib/py ~/.claude/scripts/production/targeted_test.py --repo <worktree>` (`--module <suite/module>` while iterating).
- **Lint:** `basedpyright` on each changed Python file: zero errors, zero warnings.
- **Invariants:**
  - A lookup that cannot prove the recipient still fails with its current reason; a fix never sends to a guessed recipient.
  - Every time budget is a `config/*.conf` key, never a literal in code.

## Phases

### Phase 1 — Find and remove the cause of the slow cold lookup  · status: done

As built (checkpoint 17febddac8, merge 59a6145a0b, repair fd71e67a79):

- A unit name, a local `session:` ID and a local worker's `codex:` ID resolve before any SSH peer read. The `<name>-unit` fallback runs before `matching_recipients`. `_local_worker` proves a worker is on this machine.
- `_send_with_review` reads a focused `MessageParticipants` roster for those local targets. A `--skip-closed` lookup reads `AllRosterSessions`, so it never reports an absence that it cannot prove.
- Cold lookup on natedev, before and after: unit name 4.05 s to 0.99 s; local `session:` 0.87 s to 0.56 s; local `codex:` 0.76 s to 0.58 s. Unknown names still read the peer (about 3 s).
- No total deadline: the showrunner dropped it (11-21 10:16 EST) because its caller, the build-hold enforcer, no longer exists. `config/send.conf` and `roster.py` are unchanged.
- Tests: rows in `scripts/message/test_send.py` and `scripts/message/test_roster.py`. As-built: `docs/as-built/session-roster.md`.

### Phase 2 — Pushes the user asks for skip review  · status: done

As built (List item 163; checkpoint 776c986720 on `natedev-postmaster-pushask`, tip 3101dabe4e, merged to main as b1784d7bbe):

- `scripts/message/send.py` takes `--user-asked` only with `--to user`. It passes the flag to `scripts/notify/pushover.py` on the local path and on the forwarded path (`remote`). A `--user-asked` push skips the `wrong_template` hold.
- `pushover.py` refuses `--user-asked` with exit 2 unless the text id is `push_catalog.NATE_ADHOC_TEXT_ID` (`nate.adhoc`).
- With `--user-asked`, `pushover.send` claims no hold and runs no live review. It posts the draft as written and logs `review.Unreviewed("user asked")`. Silence, priority and `--through-silence` are unchanged.
- `nate.adhoc` without `--user-asked` keeps its review.
- Docs: `commands/nate.md` adhoc sends with `--user-asked`; `commands/message.md` says to pass it only when the user asked for this push in this session.
- Tests: rows in `scripts/notify/test_pushover.py` and `scripts/message/test_send.py`.

### Phase 3 — A push text that nate approves in words passes review  · status: todo

List item 180 (producer morty). Design: `/session/postmaster/run/1965ff92-8600-4c23-8a41-ad0d87146d77/l180_design.md`. The hook change and the nate_style rule start only on nate's go typed in the postmaster pane. The security unit reads the design before code.

#### Work Order

**Goal:** A push text that nate approves in ordinary words, in the session that showed it to him, passes the review with no objection. He types no command and no text id.

**Spec:** The design and its security read are in `/session/postmaster/run/1965ff92-8600-4c23-8a41-ad0d87146d77/l180_design.md`. Read it first. This pass does steps 1 to 5. Steps 6 to 8 wait for nate's go and come in a later note.

1. **Ask.** Add `review.py texts --ask --id <id>`. It prints each rendered example, as `texts` does, with no reviewer call. It writes one ask record per herdr pane (`HERDR_PANE_ID`): text id, `push_texts.fingerprint(entry)`, session id (`CLAUDE_CODE_SESSION_ID`), time. A new ask replaces the old one. Outside herdr, or with no session id, it refuses with exit 2 and a clear reason. Put the state under the notify state folder (`NOTIFY_STATE_DIR`, default `~/.local/state/notify`): `text-asks/<pane>.json`, `text-approvals/<id>-<fingerprint>.json`.
2. **Answer decision.** New module `scripts/notify/text_asks.py` with one entry function for the prompt hook: `answer(session_id, pane, prompt, source_typed, now) -> outcome`. In order:
   a. If no ask file exists for the pane, return at once. This is the only cost for every other prompt: one stat call.
   b. Refuse unless the prompt source is TYPED (the caller passes it), the ask's session is this session, and the ask is younger than the new key `TEXT_ASK_MINUTES` in `config/notify.conf` (follow that file's comment style; read through `scripts.lib.conf`).
   c. Refuse if `script_prompts.typed_by_script(pane, prompt)` matches.
   d. Refuse unless `herdr pane get <pane>` reports `focused: true` and its `agent_session.value` equals the session id. Any herdr error refuses.
   e. Words: accept when the first word is yes, go, ok, approve or approved (case-insensitive, punctuation ignored), and no negation appears anywhere (not, n't, no, don't, never). Any other answer leaves the ask open and records nothing.
   f. On accept: write the approval record (text id, fingerprint, nate's exact words, session, pane, time, `source: user-prompt`), then delete the ask.
   Return a typed outcome for each case (no ask, refused with reason, left open, approved). No bare bool or string.
3. **Record.** `review.py texts --approve --id <id>` without `--user-spec`: when an approval record exists for the id and its current fingerprint, write each example's approval to `push_texts_reviewed.json` with `reviewer: "user-prompt: <words> (<session>, <time>)"` and `objections: []`. Call no reviewer model for that entry. Print one line that names the words. Without a record, the path is unchanged.
4. **Typing records.** In `scripts/herdr/herdr.py`, every text an agent path types is recorded in `script_prompts` before it is sent, not only slash commands. This includes `answer_question` (it sends raw `send-text` with no record today). Keep `script_prompts.record` writing before the send. Check that `conversation_pause.prompt_source` still classifies correctly: a script-typed plain text is now SCHEDULED.
5. **Tests.** Add rows to the existing tests (`scripts/notify/test_review.py`, `scripts/lib/test_script_prompts.py` or the module beside each changed file, a new `scripts/notify/test_text_asks.py`):
   - each refusal case in step 2, with a fake herdr reader and a fixture config (never the live config value);
   - nate's real answers "yes i approve it", "yes", "go" approve; "no", "don't approve", "yes but not that one" do not;
   - no ask file: the function does no herdr call and no other read;
   - `texts --approve` with a matching record writes the user-prompt approval and calls no runner; with a stale fingerprint it uses the normal review path;
   - `answer_question` writes a script record before its `send-text`.
6. (After nate's go) Hook wiring: `scripts/hooks/user-prompt-submit-conversation-pause.py` calls `text_asks.answer`.
7. (After nate's go) `settings.json` `permissions.deny` rows for raw `herdr pane send-text`, `herdr pane send-keys`, `herdr pane send-input`, `herdr pane run`, `herdr agent prompt` and `herdr agent send-keys`.
8. (After nate's go) One rule in `~/rust/nate_style/message/`: a text nate dictated or approved in words is exempt from the reviewer's rules.
9. **Docs.** `docs/as-built/message-clarity.md` where it states how push texts get approved. The `review.py` docstring and usage for `--ask`.
10. **Nothing else.** Add no other guard, check, config key or retry. This path covers push texts only.

**Files:** `scripts/notify/review.py`, `scripts/notify/test_review.py`, `scripts/notify/text_asks.py` (new), `scripts/notify/test_text_asks.py` (new), `scripts/herdr/herdr.py` and its test module, `scripts/lib/script_prompts.py` and its test module (only if needed), `config/notify.conf`, `docs/as-built/message-clarity.md`.

**Acceptance gate:**
- `targeted_test.py --repo <worktree>` passes, except a red that also fails on the base commit.
- basedpyright reports zero errors, zero warnings and zero notes on each changed Python file.

### Phase 4 — The send log keeps a changed relay call  · status: done

As built (checkpoint 74dd0dc24e on `natedev-postmaster-relaylog`, which contains main as of 23:45 EST):

- `read_relay` in `scripts/message/send.py` checks every SendMessage call before its forward checks and early returns. When the relay's `to` or `message` differs from the approved values, `_log_relay_mismatch` appends one JSON line to `STATE/relay_mismatches.jsonl` on the relay host: the relay's target (capped at 256 characters, with `to_truncated`), and 32-character windows of the approved and sent text around the first difference.
- A write failure prints one line to stderr and never blocks the relay. The outcome, the printed line and the exit code stay the same.
- Tests: rows in `scripts/message/test_send.py` read `relay_mismatches(state)`. As-built: `docs/as-built/session-roster.md`.

### Phase 5 — Hooks without a signal handler  · status: done

As built (checkpoint 416bb6a5f4 on `natedev-postmaster-nosig`, which contains main as of 00:37 EST):

- `scripts/hooks/stop-conversation-pause.py` and `scripts/hooks/user-prompt-submit-conversation-pause.py` install no signal handler. Each `__main__` calls `main(budgeted=True)`; direct calls to `main()` run in the caller's process.
- `scripts/hooks/hook_budget.py` (standard library only) starts the deadline before the hook imports `conversation_pause`. It forks a `setsid` supervisor, which forks the worker. The supervisor ends the worker group through `scripts/lib/process_group.send_process_group` at the deadline or when the hook's owner pipe closes, and removes the worker's `.scheduled-prompts-<supervisor>-<worker>-*` temporary files.
- At the deadline the hook prints `conversation-pause: HookBudgetSpent: gave up after N seconds` and exits 0, as before. `CONVERSATION_PAUSE_HOOK_BUDGET` still sets the budget (default 5 s).
- Tests: `scripts/hooks/test_conversation_pause_processes.py` (no signal handler, stalled import, temporary-file cleanup, killed hook ends its worker). As-built: `docs/as-built/conversation-pause.md`.

### Phase 6 — A relay delivers the approved text  · status: done

As built (checkpoint c5d84e9c32 on `natedev-postmaster-relayfix`, tip 18765b08b2 with main 939fc216ed):

- Cause: the relay's model sometimes changed its SendMessage call. On 11-22 it dropped the lines after the body (from `Sender native id:` on) or added a space to the summary. The guard blocked the call, and the send was REFUSED or queued.
- `RELAY_TOOL_GUARD` in `scripts/message/send.py` approves the first SendMessage call and prints a PreToolUse `updatedInput` that sets `to`, `message` and `summary` to the approved values. A later call, another tool or a missing tool id exits 2.
- `read_relay` accepts a call when the proxy forwarded the approved text, even when the model's input differs. The detail adds `guard set the approved input`. A changed call that was not forwarded stays REFUSED.
- `STATE/relay_mismatches.jsonl` still records each changed model input, now with `summary_changed`.
- Tests: rows in `scripts/message/test_send.py`. As-built: `docs/as-built/session-roster.md`.

### Phase 7 — A long relay message does not time out  · status: done

As built (checkpoint 2683705a47 on `natedev-postmaster-relaybig`, base 5e02a261da with main 99f0deddd1):

- Cause: the relay model typed the whole message into its SendMessage call, so a 22 KB message hit `relay timed out after 40 s`.
- `relay_prompt` in `scripts/message/send.py` carries the target, the summary and `RELAY_MESSAGE_STAND_IN` ("approved text"), not the message. The guard sets the approved message.
- `read_relay` and `_log_relay_mismatch` compare the model's `message` with the stand-in. The proxy forward check against the approved text is unchanged.
- Live proof: a 22,231-char message arrived whole in 8.7 s, with no `guard set` detail.
- Tests: rows in `scripts/message/test_send.py`. As-built: `docs/as-built/session-roster.md`.

### Phase 8 — A one-line push text  · status: done

As built (checkpoint 6e39bb203c, stacked on phase 7 on `natedev-postmaster-relaybig`):

- `PushText.one_line` in `scripts/notify/push_texts.py`. `catalog()` refuses it on an entry with an action, a session or a body. `fingerprint` includes it only when True.
- `RenderedPush.posted_title` is empty and `posted_body` is the rendered title for a one-line text. `scripts/notify/pushover.py` leaves out the Pushover `title`; the phone shows the app name. The `Sent by:` footer stays.
- `review.py` approval drafts and `--ask` output show the text as posted.
- No live entry sets the flag. Accounts sets the entries after nate's words.
- Tests: `scripts/notify/test_push_texts.py`, `test_pushover.py`, `test_review.py`. As-built: `docs/as-built/message-clarity.md`.
- Gate: the showrunner asked for direct tests instead of the queue (11-22 17:44 PST). 718 tests in `scripts.message.test_send` and all `scripts/notify` test modules passed. The merge gate is the full proof.

### Phase 9 — An announcement goes to live sessions only  · status: done

List item 268 (nate 11-23 14:08 PST). `broadcast.py --all` printed 887 lines: 59 sent and 824 closed workers.

As built (commit 2e4efe5d53, phase livecast, on main by 892cb2d74f on 11-24):

- `scripts/production/broadcast.py` leaves closed worker rows out of every group: no send attempt and no output line. Stopped, unknown and incomplete workers keep their line.
- `scripts/message/roster.py` keeps confirmed closure as its own typed state. The roster row and the send.py refusal text do not change.
- Tests: `scripts/production/test_broadcast.py`, `scripts/message/test_roster.py`. Doc: `docs/as-built/session-roster.md`.

### Phase 10 — Approved push texts go into the catalog  · status: done

As built (checkpoint 0b8bbb4980, branch `postmaster-textcat`, stacked with phase 11):

- `scripts/notify/push_catalog.py` carries the texts nate approved on the Images page: quota.claude_expiry, mac_idle.unreachable, nightly.result, nixos.gpu_stopped, nixos.ups_no_contact, nixos.ups_power_back, nixos.ups_replace_battery, nixos.ups_shutdown, silence.auto_off, silence.auto_on. `push_texts_reviewed.json` records each approval with nate's words, place and time.
- `hide_sender_values` raises when a hidden sender value is missing; `silence_auto_off_values` raises on an unknown held sentence; `mac_idle_unreachable_values` keeps `minutes` for the sender.
- `scripts/notify/push_texts.py` renders no trailing newline for an empty body.
- nixos.ups_battery and quota.codex_workers_moved stay as on main: their approved titles exceed the 44-character phone title. Shorter titles went back to nate.
- Tests: `scripts/notify/test_push_texts.py`, `test_pushover.py`, `scripts/whoami/test_quota_alert_expiry.py`.
- Gate: `--affected`, Mac Linux guest gate passed, 697 modules (11-24).

### Phase 11 — A production announcement survives a passing unreadable record  · status: done

As built (commit 3d4a6fc921 on `postmaster-retry`, stacked as checkpoint 0b8bbb4980):

- The `--production` path in `scripts/production/broadcast.py` reads the roster a second time when each roster problem is an unreadable session record, on either machine. The wait before the second read comes from `config/session_retries.toml`. A record still unreadable stops the send, as the staff-copies phase 52 rule requires. Any other problem stops it at once.
- `scripts/message/roster.py` names the unreadable-record problem as a typed value.
- Tests: `scripts/production/test_broadcast.py` (recovers on the second read with a recipient seen only then, stays blocked, other problem stops at once; the sleep uses the configured wait), `scripts/message/test_roster.py`.
- Gate: stacked with phase 10, 697 modules passed (11-24).

### Phase 12 — The auto mode refusal push is short  · status: done

Item 221. nate approved the auto_mode.refused before/after pair on the Images page (07:37 PST Nov 24).

As built (commit ae4642959e, stacked with phase 13 as 0bee23a714, on main as e75528b945 on 11-24):

- auto_mode.refused shows "<session>: <count> <noun> refused; <what waits>" and the action line only. The command list, the Go and Allow rule sentences and the sender prefix are gone. RCA still gets each refused command in full.
- The waits clause stays in the title when it fits in 44 characters; the 30 s review judges only that clause.
- `scripts/message/send.py` asks for `waits` in the title form. The approval is in `push_texts_reviewed.json`.
- Tests: `scripts/notify/test_push_texts.py`, `test_pushover.py`, `scripts/message/test_send.py`.

### Phase 13 — The UPS battery push is short; dead shutdown texts go  · status: done

Item 221: nate approved the nixos.ups_battery before/after pair on the Images page (09:49 PST Nov 24). List items 330 and 331: dwight decided to delete five shutdown texts that no code sends (09:21 PST Nov 24).

As built (commit 4c60b6b40f, stacked with phase 12 as 0bee23a714, on main as e75528b945 on 11-24):

- nixos.ups_battery shows "Power out: natedev on battery, <minutes> min left" and the action "Save your open work on natedev now." with no body. The sender still passes `charge`; the push does not show it.
- The catalog loses shutdown.move_failed, restart_incomplete, restart_untrusted, restart_untrusted_unlisted and manual_restart, with their approvals and tests.
- Tests: `scripts/notify/test_push_texts.py`, `test_pushover.py`, `test_review.py`.

### Phase 14 — The review watch push goes  · status: done

Item 221: nate, Images page, 10:28 PST Nov 24, on review_watch.acknowledge: "this looks like a one off that should just be removed".

As built (commit c88a139532, branch `postmaster-nowatch`, on main as 80ccf6437a on 11-24):

- Removed: the review_watch.acknowledge text and approval, its push reference kind and silence branch, `ci_points.py watch`, `review_regime.py watch` and `ack`, and the showrunner watch step.
- The review ledger (`review_regime.py add` and `report`) and `ci_points.py due` stay. The as-built docs say the watch is removed.
- Tests: the tests of the removed parts are deleted; `scripts/production/test_ci_points.py`, `test_review_regime.py`, `scripts/notify/test_pushover.py` keep the rest.

### Phase 15 — The nightly assigned push is short  · status: done

Item 221: nate approved the nightly.assigned pair (10:33 PST Nov 24) and the nightly.not_run text (11:21 PST Nov 24) on the Images page.

As built (commits 30f8b4f25b and 85f64814a7, branch `postmaster-nowatch`, on main as 80ccf6437a on 11-24):

- nightly.assigned shows "Nightly check failed" and one line, "Fix assigned to: <unit>.", with no action line.
- nightly.not_run shows "Nightly check did not run", a Why line and a Next run line. `scripts/production/nightly_schedule.py` reads the next start of claude-nightly.timer. A fetch failure has no action line; cold credentials keep the github-warmup action.
- An `ActionTemplate` can omit an empty action line; the review.py approval preview omits it too.
- Tests: `scripts/notify/test_push_texts.py`, `test_pushover.py`, `test_review.py`, `scripts/production/test_gate_suites_types.py`.

### Phase 16 — The AWS refused read push names one short command  · status: todo

Item 221, List item 359: nate approved the aws.snapshot_failed text in brain's review (about 11:10 PST Nov 24). It goes live only after the `aws-deploy` command exists on natedev (builds, aws unit).

#### Work Order

**Goal:** The typical push renders the title "AWS refused a price read", the action "In a terminal on natedev, run aws-deploy. It asks for your AWS admin passphrase." and the body "AWS refused the us-east-2 price read, because our AWS setup does not allow it yet. The command adds the permission. Without the read, AWS merge tests skip that region after 2 hours."

1. `scripts/notify/push_catalog.py` and `scripts/aws_target/aws_target.py`: the sender gives the read and the region of each refused read. One refused read renders the approved form; several name each read in the same form.
2. The sentence "The command only allows these reads and costs nothing" goes.
3. Approval record: `review.py texts --approve --id aws.snapshot_failed` with nate's words.
4. Tests: rows for the approved text.

**Acceptance gate:** `copies.py run ... gate.py --affected` passes; basedpyright clean on each changed file. Merge only after `aws-deploy` exists on natedev.

### Phase 17 — Six approved push texts go into the catalog  · status: done

Item 221: nate approved six texts on the Images page (Nov 24).

As built (commit 9b0213d940, stacked on `postmaster-nowatch`, on main as 80ccf6437a on 11-24):

- quota.codex_workers_moved, paid_credits, paid_credits_queued and stall_watch.permission_prompt use the approved texts. Each title fits 44 characters.
- A paid-credit push held by silence builds the /tune account list again from live config at release. A failed read uses the saved list and logs one warning.
- Tests: `scripts/notify/test_push_texts.py`, `test_pushover.py`, `scripts/production/test_stall_watch.py`, the `scripts/whoami/test_quota_*` modules.

### Phase 18 — The needs_go push names the List item to press Go on  · status: todo

Item 325 and item 221: nate approved the session.needs_go before/after pair on the Images page (image b09de2d3d949, 11:23 PST Nov 24).

#### Work Order

**Goal:** The session.needs_go push renders the title "trunk waits for your go", the line "Session: trunk", and the action "Press Go on List item 412: remove arrange_jacks/ from phases/130/files; 140 owns it". No sender prefix, no pane, no line to type, no reason and no "what the go does".

1. Base: the needsme branch (List item 232: the List page Go button and `the_list.py ask`). The hash merges only with or after needsme.
2. `send.py` session.needs_go takes the List item number of the open go ask. It refuses the send when that item has no open go ask (`the_list.answerable_asks`).
3. Catalog text and approval record with nate's words; tests; docs for the new form.

**Acceptance gate:** `copies.py run ... gate.py --affected` passes; basedpyright clean on each changed file.

### Phase 19 — /codex_winddown and its codex.reset_ready push go  · status: done

List item 326: nate asked to remove /codex_winddown (11:31 PST Nov 24).

As built (commit 3585399dde, branch `postmaster-nowinddown`, on main as 50b18f9c62 on 11-25):

- The command file, `scripts/production/codex_winddown.py` and its tests are deleted. The codex.reset_ready push and its approval record go. `process_table.is_codex_agent` and `live_units.LiveUnitIds`/`live_unit_ids` lost their only caller and are deleted.
- Silence release drops a kept push whose template is not in the catalog and logs "dropped: retired template <id>".
- Shared checks moved to `scripts/message/test_sessions.py` and `scripts/production/test_broadcast.py`.

### Phase 20 — shutdown.restart_failed names the session and the resume command  · status: done

List items 327 and 331: nate approved the text in brain's review ("yes", 11:41 PST Nov 24).

As built (commits 6e6600788d and 508f4d2237, branch `postmaster-norcapush`, on main as 50b18f9c62 on 11-25):

- The title is "<session> did not start on <account>"; the action is "In the natedev session, run: /agents resume <session> --to <account>"; no body. With no name or an ambiguous name, the action names the session id. A long name ends in "…".
- A unit that its watchdog starts again gets no push; the watchdog sends the push once when the start fails.
- `/agents resume` on natedev reaches a session hosted on the Mac over ssh. A named resume refuses a row with no machine or no route, with its reason.
- A kept restart_failed push renders again at silence release, or drops with one log line when it lacks the session.
- Tests: `scripts/agents/test_stop_resume.py`, `test_shutdown_cli.py`, `test_pause_exit.py`, `test_shutdown_status.py`, `scripts/notify/test_push_texts.py`, `test_pushover.py`.

### Phase 21 — Every Builds page run shows its unit and its showrunner  · status: todo

List item 373: nate, Nov 24 13:10 PST: "handler doesn't show a showrunner in its tooltip - that should be impossible - it's an invariant". Sent by builds.

#### Work Order

**Goal:** every run on the Builds page resolves to its unit and its showrunner. A known unit never shows fallback text.

1. Find why the handler row (Codex worker impl2 of unit cargo-liner/handler, session 01a1d62e-5f73-7b80-b00e-1842d70c3a4d, Mac run of `cargo build --locked --release -p cargo-berth`) shows "Caller: unregistered session" and no showrunner. On natedev, `unit_labels.unit_labels_for` resolves the session to cargo-liner/handler.
2. Fix the cause where it lives. Add no fallback text for a known unit.
3. Prove it on live-shape data (builds_page_real_rows.json shape) and take one dark desktop shot of the tooltip.

**Acceptance gate:** `copies.py run ... gate.py --affected` passes; basedpyright clean on each changed file; the shot shows the handler tooltip with unit and showrunner.

### Phase 22 — rca.py sends no "finding why" or "fixed why" push  · status: done

List item 330: nate, Nov 24 13:28 PST: "okay no push - only the first is fine".

As built (commits 3d1df89bc5 and f82023bd6b, branch `postmaster-norcapush`, on main as 50b18f9c62 on 11-25):

- The first permission alert stays. `rca.py start` records the owner; `rca.py fixed` records the cause, fix and commit and retires the alert. Neither sends a push.
- `rca.py fixed` exits 1 when the alert retirement fails and says the alert can repeat; a second call retries.
- permission.rca_started, rca_fixed and rca_fixed_rewritten, their review records and the send.py rows are removed. Silence release drops kept pushes of these templates.
- Tests: `scripts/message/test_rca.py`, `test_send.py`, `scripts/notify/test_pushover.py`. Doc: `docs/as-built/message-clarity.md`.

### Phase 23 — No phone text shows "No action needed."  · status: done

List 221: nate, Nov 22: 'don't say"No action needed" i already know that - just elide it'. natedev, Nov 27 03:06 PST: go for parts 1, 3 and 4. The nate.adhoc action field is dropped (natedev 03:07 PST).

As built (tip ab8abb95b6, branch `postmaster-noaction`, on main as e42c959226 on 11-27):

- `scripts/notify/user_action.py` owns the header rule: `notification_header()` and `notification_message()` replace `first_line()`. No action gives only the session line, or no header. Leading whitespace-only lines go. `LEGACY_NO_ACTION_LINE` lets old records parse.
- `scripts/notify/pushover.py`, `review.py`, `silence.py` and `scripts/message/send.py` join the header and body through these functions. `review.py check` and `replay` print the phone header; `replay` reads old log lines, legacy actions and sampled replies.
- Catalog texts and their approvals do not change: 45 of 45 fingerprints, 89 approvals match. No `review.py texts --approve`.
- Stated limit: an old record with no header field and an "Action:" body line replays as an action, as main did before.
- The nate_style guide `message/push-body-under-500.md` counts only the lines that appear (nate_style 6587583).
- Tests: `scripts/notify/test_pushover.py`, `test_review.py`, `test_push_texts.py`, `test_silence.py`, `scripts/message/test_send.py`, `scripts/hooks/test_push_gate.py`, `scripts/whoami/test_quota_zero_episode.py`.
- Gate: `--affected` green on ab8abb95b6 (11-27); behavior check exit 0, key 5c2c4358.

## Acceptance gate

- basedpyright reports zero errors, zero warnings and zero notes on each changed Python file.
- Each tip passes `copies.py run ... gate.py --affected` before its hash goes out.

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

## Acceptance gate

- basedpyright reports zero errors, zero warnings and zero notes on each changed Python file.
- Run no test of any kind now: the showrunner holds all test runs on natedev. I run the gate later.

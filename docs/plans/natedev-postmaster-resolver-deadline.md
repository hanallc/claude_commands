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

#### Work Order

**Goal:** A cold `send.py` recipient lookup returns in a short time. The cause of the present lookup time of about 20 s is measured and removed. A total lookup deadline exists only if a lookup can still run long after that fix.

**Spec:**

The request came from sdlc's devtools through the sdlc and natedev showrunners. The build-hold enforcer now resolves recipients before it sends TERM, so the TERM waits for the lookup. A cold peer lookup takes about 20 s for each recipient.

1. **Measure.** Reproduce a cold lookup on natedev. "Cold" means the first lookup in a new process. Time each step inside `resolve_recipient()` and the calls under it, for example with `time.perf_counter()` probes in a scratch copy or `python -X importtime`. Do not use cProfile for the time figures: it inflates them. Name the step or steps that use the time. Record the measured figures, the command and the cause in your reply.
2. **Fix the cause.** Change the slow step so that a cold lookup is fast. Examples: a remote call or a subprocess that runs before a local match could answer, a full scan that a narrower read can replace, or a retry or sleep in the cold path. Keep the lookup result the same for every input.
3. **Deadline, only if needed.** After step 2, if a lookup can still run long (for example, a remote machine that does not answer), add one total deadline to the resolver. Read the value from a new key in `config/send.conf`, with a `# range:` comment like the other keys. When the deadline passes, the lookup fails with a clear reason that names the recipient and the deadline. If no long path remains, add no deadline and say why in your reply.
4. **Nothing else.** Add no other guard, check, cache layer or retry.
5. **Tests.** Add rows to the existing tests in `scripts/message/test_send.py` (or the module that owns the changed code). One row proves the slow step no longer runs on the cold path. If step 3 adds a deadline, one row proves the deadline failure and its reason, with an injected clock or a fixture config, never a real wait.
6. **As-built.** Update `docs/as-built/session-roster.md` where it describes recipient lookup, so it states how the lookup works now.

**Files:**
- `scripts/message/send.py`, `scripts/message/test_send.py` — resolver
- `scripts/message/roster.py`, `scripts/message/sessions.py`, `scripts/message/test_roster.py` — only if the slow step lives there
- `config/send.conf` — only if step 3 adds a deadline
- `docs/as-built/session-roster.md` — lookup description

**Workers:** 1 writer — `impl` measures first, then holds every file; the cause decides which files change.

**State:** none

**Constraints from prior phases:** none

**Acceptance gate:**
- The reply gives the cold lookup time before and after the fix, measured with the same command.
- `targeted_test.py --repo <worktree>` passes, except a red that also fails on the base commit.
- basedpyright reports zero errors and zero warnings on each changed Python file.

### Phase 2 — Pushes the user asks for skip review  · status: todo

List item 163. The code is on branch `natedev-postmaster-pushask` (worktree `~/worktrees/claude-natedev-postmaster-pushask`, from origin/main 94e07d22c4), so its hash merges apart from phase 1.

#### Work Order

**Goal:** A push the user asks for with `/nate adhoc` goes out as the agent wrote it, with no live review, hold or rewrite. Silence rules still apply. Every other push keeps its review.

**Spec:**

The user, 2026-11-21 (typed "go" in the postmaster pane): "go" for the pushes that i ask for explicitly without review. Earlier user rule, 2026-11-13: "if i asked for it it always should sent, it just needs to honor the silence rules".

1. **Flag.** Add `--user-asked` to `scripts/message/send.py` for `--to user`. It passes through to `scripts/notify/pushover.py`, also on every path that forwards or delays send arguments (the same paths that carry `--through-silence`, for example `send.py` near lines 4352 and 4494, and `send_later.py` if it rebuilds arguments). `send.py` refuses `--user-asked` without `--to user`, the same way it refuses `--through-silence`.
2. **Accept only with nate.adhoc.** `pushover.py` accepts `--user-asked` only with `--text-id nate.adhoc`. With any other text id it refuses with exit 2 and a clear reason. Read the text id from the catalog name, not a literal spread in several places.
3. **No review.** With `--user-asked`, `pushover.send` runs no live review, no hold claim and no rewrite. It sends the draft as written. It records the push as unreviewed with the detail `user asked`, through the same `review.Unreviewed` and `log_review` path that `review: off` uses. Silence policy, priority and `--through-silence` stay as they are.
4. **Agent notes keep review.** `nate.adhoc` without `--user-asked` keeps the current review path unchanged.
5. **Docs.**
   - `commands/nate.md` adhoc: add `--user-asked` to the send arguments, including delayed arguments. Replace "Review still runs" and the reviewed-push paragraph: the push goes out as written; silence still keeps it unless `through silence`.
   - `commands/message.md`: one line: never pass `--user-asked` unless the user asked for this push in this session.
   - The usage text and module docstrings of `send.py` and `pushover.py` that list `--through-silence`.
   - `scripts/notify/reviewer_prompt.md` only if it states that every adhoc push is reviewed.
6. **Nothing else.** Add no other guard, check or config key.
7. **Tests.** Add rows to the existing tests:
   - `scripts/notify/test_pushover.py`: `nate.adhoc` with `--user-asked` posts the draft unchanged and calls no review; the same push during silence without `--through-silence` stays kept; `--user-asked` with another text id is refused.
   - `scripts/message/test_send.py`: `--user-asked` reaches the pushover command line; `--user-asked` without `--to user` is refused.

**Files:** `scripts/notify/pushover.py`, `scripts/notify/test_pushover.py`, `scripts/message/send.py`, `scripts/message/test_send.py`, `scripts/message/send_later.py` (only if it rebuilds arguments), `commands/nate.md`, `commands/message.md`, `scripts/notify/reviewer_prompt.md` (only if needed).

**State:** none

### Phase 3 — A push text that nate approves in words passes review  · status: todo

List item 180 (producer morty). Design: `/session/postmaster/run/1965ff92-8600-4c23-8a41-ad0d87146d77/l180_design.md`. The hook change and the nate_style rule start only on nate's go typed in the postmaster pane. The security unit reads the design before code.

#### Work Order

**Goal:** A push text that nate approves in ordinary words, in the session that showed it to him, passes the review with no objection. He types no command and no text id.

**Spec:** The design and its security read are in `/session/postmaster/run/1965ff92-8600-4c23-8a41-ad0d87146d77/l180_design.md`. Read it first. This pass does steps 1 to 5. Steps 6 to 8 wait for nate's go and come in a later note.

1. **Ask.** Add `review.py texts --ask --id <id>`. It prints each rendered example, as `texts` does, with no reviewer call. It writes one ask record per herdr pane (`HERDR_PANE_ID`): text id, `push_texts.fingerprint(entry)`, session id (`CLAUDE_CODE_SESSION_ID`), time. A new ask replaces the old one. Outside herdr, or with no session id, it refuses with exit 2 and a clear reason. Put the state under the notify state folder (`NOTIFY_STATE_DIR`, default `~/.local/state/notify`): `text-asks/<pane>.json`, `text-approvals/<id>-<fingerprint>.json`.
2. **Answer decision.** New module `scripts/notify/text_asks.py` with one entry function for the prompt hook: `answer(session_id, pane, prompt, source_typed, now) -> outcome`. In order:
   a. If no ask file exists for the pane, return at once. This is the only cost for every other prompt: one stat call.
   b. Refuse unless the prompt source is TYPED (the caller passes it), the ask's session is this session, and the ask is younger than the new key `text_ask_minutes` in `config/notify.conf` (follow that file's comment style; read through `scripts.lib.conf`).
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
- Run `gate.py --affected --repo <worktree>`. A failure is acceptable if it also fails on the base commit.
- basedpyright reports zero errors, zero warnings and zero notes on each changed Python file.

# Postmaster relay prefix

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** Relayed messages start with the sender name and the text, and a direct message to postmaster reaches postmaster.

> **As-built disposition: amend** — `docs/as-built/session-roster.md`

## Delegation Context

- **Project:** `~/.claude` scripts — the send.py message path and the postmaster mod that delivers its outbox and forwards answers.
- **Project started:** 2026-11-18T15:43:12.932+00:00
- **Stack:** Python 3 (basedpyright strict, unittest); TypeScript mod on the Claude Code engine (`bun test`, `tsc`).
- **Layout:** `scripts/message/` (send.py and tests), `scripts/direct/` (progress_history.py), `scripts/hooks/`, `scripts/production/`, `scripts/style/`, `mods/postmaster/` (hooks and tests), `commands/`, `docs/`.
- **Key files:**
  - `scripts/message/send.py` — `delivered_text()` builds the text postmaster delivers; `default_sender()` gives `send.py in <session>`; `stamped_text()` puts `Sender native id:` after the first line; `shown_sender_line()`.
  - `mods/postmaster/hooks/replies.ts` — `receive()` takes a peer message from a socket with an open delivery route as an answer, logs it and forwards it with a header.
  - `scripts/direct/progress_history.py` — the turn-wait reader strips the delivery header with a regex before it checks for `From the user (via the showrunner):`.
  - `scripts/production/merge_checkpoint.py`, `scripts/style/style_sync.py` — send hand-off and refill notices with `--from <production name>`.
- **Test lanes:** Python: the `test_*.py` beside each changed script. Mod: `mods/postmaster/tests/`.
- **Build:** `nix shell nixpkgs#typescript --command tsc --noEmit -p mods/postmaster`; `command claude plugin validate --strict mods/postmaster`
- **Test:** `~/.claude/scripts/lib/py ~/.claude/scripts/production/targeted_test.py --repo <worktree>` for Python (`--module <suite/module>` while iterating); `command claude plugin test mods/postmaster` for the mod.
- **Lint:** `basedpyright` on each changed Python file: zero errors, zero warnings.
- **Invariants:**
  - Each delivery keeps a distinct tag in its text, so the recipient's repeat-text filter does not drop equal messages (as-built `docs/as-built/session-roster.md`).
  - `From the user (via ...)` lead-ins stay: they mark the user's words (`docs/production_format.md` → `<ProductionUnit/>` item 2).
  - The mod writes each record file once (engine `$.fs` has no rename).

## Phases

### Phase 1 — Short relay prefix and direct messages to postmaster  · status: done

#### Work Order

**Goal:** A relayed message's preview line reads `<sender>: <text>`, a forwarded answer reads `<sender> (answer to <original>): <text>`, showrunner lead-ins are gone where the sender name shows, and a message to postmaster that starts with `postmaster:` reaches the postmaster session.

**Spec:**

The user (via natedev, 2026-11-18) saw `Message from @postmaster: Message from send.py in hana via postmaster: From the showrunner: fold #112 Phase 7 end is now about 02:... [2d0ea222]` and wants `message from hana: fold #112 ...`. Claude Code writes `Message from @<session>:` from the sending socket, and the engine send call has no sender field, so that part stays `@postmaster`. Everything after it is ours.

1. **send.py delivery text.** `delivered_text()` today gives
   `Message from <sender> via postmaster: <summary> [<tag8>]` + optional `Shown sender:` line, then `Sender native id:` (inserted by `stamped_text`), a blank line and the text. Change it to:
   - first line `<name>: <summary>`, where `<name>` is the sender with a leading `send.py in ` removed (`send.py in hana` → `hana`; a bare `send.py` or any other `--from` value stays as it is). Reuse an existing helper if send.py has one for this (the relay path calls `relay_session_name`); add none if one fits.
   - the tag leaves the first line. Add the line `Tag: <tag8>` after the `Shown sender:` line (or after `Sender native id:` when there is no shown sender). The tag value and its source (`entry_id[:8]`) do not change.
   - Everything else stays: the `Sender native id:` line, the `Shown sender:` line rule, the blank line and the full text.
2. **Forwarded answers (replies.ts).** The forward text today is
   `Message from <name> (their answer to <answerTo>, passed on by postmaster) [<at_ms>-<n>]:` then a blank line and the body. Change it to
   `<name> (answer to <answerTo>): <body>` then a blank line and `Tag: <at_ms>-<n>`. `answerTo` keeps its `one of A, B` form for several senders. The key value and the reply/forward log records do not change.
3. **Direct message to postmaster.** In `receive()`, after `wrapper()` parses a peer message and before any route lookup, a body whose first non-space text is `postmaster:` (case-insensitive) returns `{ kind: "pass" }`: it reaches the postmaster session and no reply or forward record is written. All other routing stays.
4. **Header readers.** `progress_history.py` (the regex before the `From the user (via the showrunner):` check) and `scripts/hooks/test_direct_turn_wait_hooks.py` match the old header. Change them to strip the new delivery header: first line `<name>: <summary>`, the `Sender native id:`, optional `Shown sender:` and `Tag:` lines, then the blank line. Search `scripts/`, `mods/` and `commands/` for every other reader of `via postmaster`, `passed on by postmaster` or the `[<8 hex>]` delivery tag and change each one the same way.
5. **Lead-ins.** Remove the lead-in where the sender name already shows:
   - `scripts/production/merge_checkpoint.py` `acknowledge_handoff`: `hand-off <c7> merged as <m7>.` (it sends `--from <production name>`).
   - `scripts/style/style_sync.py` `handoff_notice`: `hand-off <checkpoint>`; the refill notice: `ticket <id> merged; refill.` (both send `--from <name>`).
   - `commands/showrunner/produce.md`: line ~129 "beginning `From the showrunner:`" → the showrunner's coordination goes by SendMessage with no lead-in; lines ~172, ~870, ~1055 drop `From the showrunner:` / `From the showrunner of <x>:` from the quoted texts.
   - `docs/as-built/style-showrunner.md` (~604, ~664, ~696), `docs/as-built/showrunner-automation.md` (~835), `docs/style_production.md` (~601, ~618, ~634, ~648): quote the new texts.
   - Keep every `From the user (via ...)` lead-in.
   - Search `scripts/`, `commands/` and `docs/` (not `docs/plans/`) for any code that parses `From the showrunner` or `hand-off`/`ticket ... merged` texts and change it to match.
6. **Docs.** `docs/as-built/session-roster.md` (the delivery format paragraph near line 250, and the forward format) and `commands/message.md` (line ~65, "Postmaster names the real sender") state the new first lines and the `Tag:` line. `commands/message.md` also says: to reach the postmaster session itself, start the message with `postmaster:`; any other message from a session that postmaster delivered to in the last 30 minutes is forwarded as an answer.

**Files:**
- `scripts/message/send.py` — `delivered_text()`
- `scripts/message/test_send.py`, `scripts/message/test_postmaster.py` — expected header rows
- `scripts/direct/progress_history.py`, `scripts/direct/test_progress_history.py` — header regex
- `scripts/hooks/test_direct_turn_wait_hooks.py` — header prefix row
- `scripts/production/merge_checkpoint.py`, `scripts/production/test_merge_checkpoint_handoff.py` — hand-off text
- `scripts/style/style_sync.py`, `scripts/style/test_style_sync.py` — hand-off and refill texts
- `mods/postmaster/hooks/replies.ts`, `mods/postmaster/tests/replies.test.ts` — forward header, `postmaster:` pass
- `commands/message.md`, `commands/showrunner/produce.md`, `docs/as-built/session-roster.md`, `docs/as-built/style-showrunner.md`, `docs/as-built/showrunner-automation.md`, `docs/style_production.md` — texts and rules

**Workers:** 2 writers — split by language; no test lane opens before the code because each test row changes with its code.
- `impl` — every Python file above (send.py, progress_history.py, merge_checkpoint.py, style_sync.py and their tests, test_direct_turn_wait_hooks.py, test_postmaster.py).
- `test` opens as impl — `mods/postmaster/hooks/replies.ts`, `mods/postmaster/tests/replies.test.ts`, and every doc and command file above.

**State:** none

**Constraints from prior phases:** The mod writes `replies/<at_ms>-<n>.json` once before the first forward and `forwards/<at_ms>-<n>.json` = `{forwarded}` once after the last forward (main 82f47a15, dfb1ef64). Keep both.

**Acceptance gate:**
- `targeted_test.py --repo <worktree>` passes; it covers the six changed Python test modules and their callers.
- basedpyright reports zero errors and zero warnings on each changed Python file.
- `command claude plugin test mods/postmaster`, the `tsc` build and `plugin validate --strict` pass.
- A test row shows a delivery whose first line is `hana: <summary>` for sender `send.py in hana`, with the `Tag:` line present and distinct for two equal texts.
- A test row shows a peer message starting `postmaster:` from a socket with an open route returns `pass` and writes no reply record.

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

#### As-built

- A postmaster delivery (`send.py` `delivered_text()`) reads `<name>: <summary>`, then `Sender native id:`, an optional `Shown sender:`, `Tag: <entry_id[:8]>`, a blank line and the full text. A leading `send.py in ` is removed from the name (`send.py in hana` → `hana`). `MESSAGE_LINE_BREAK` turns line breaks in the name, summary and shown sender into spaces, so the header has a fixed line count.
- A forwarded answer (`replies.ts`) reads `<name> (answer to <sender, or one of A, B>): <body>`, a blank line and `Tag: <at_ms>-<n>`. The reply and forward records are unchanged.
- A peer message to postmaster whose body starts with `postmaster:` (any case, leading space allowed) returns `pass`: it reaches the postmaster session and writes no reply or forward record.
- `progress_history.py` strips the new delivery header before the `From the user (via the showrunner):` check. `PostmasterReplyRecord.matches_forward()` rebuilds the forward text from the reply record, and `_supplier_message` reads the key from the final `Tag:` line.
- Hand-off and refill notices from `merge_checkpoint.py` and `style_sync.py` have no `From the showrunner` lead-in. `From the user (via ...)` lead-ins stay.

**Files:**
- `scripts/message/send.py` — `delivered_text()`, `MESSAGE_LINE_BREAK`
- `mods/postmaster/hooks/replies.ts` — forward text and the `postmaster:` pass
- `scripts/direct/progress_history.py` — delivery header strip; forward match by record
- `commands/message.md`, `docs/as-built/session-roster.md` — delivery and forward formats, the `postmaster:` rule

**Gotchas:**
- The UI line `Message from @postmaster:` stays: the engine send call has no sender field.
- A sender name can contain `): `, so a forward is matched against its rebuilt text, never split.
- The reader has no support for the old forward format; the mod reloads about 20 s after a pull on main.

**Ruled out:** a reader for the old forward format during the reload window; a `> ` continuation form for multiline header fields.

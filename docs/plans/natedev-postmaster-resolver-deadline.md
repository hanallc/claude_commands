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

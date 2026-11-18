# No "From" lead-in in notices

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** Notice templates and script messages start with their content, because postmaster already shows the sender name first.

> **As-built disposition: amend** — `docs/as-built/showrunner-automation.md`, `docs/as-built/style-showrunner.md`, `docs/as-built/director-model-study.md`

## Delegation Context

- **Project:** `~/.claude` scripts and docs — the notice texts units and scripts send through send.py.
- **Project started:** 2026-11-18T18:05:00.000+00:00
- **Stack:** Python 3 (basedpyright strict, unittest); Markdown commands and docs.
- **Layout:** `scripts/style/`, `scripts/model_study/`, `scripts/lib/`, `commands/unit/`, `docs/`, `docs/direct/`, `docs/as-built/`.
- **Key files:**
  - `scripts/style/style_sync.py:143` — the branch-point sync notice, sent with `--from <caller>`.
  - `scripts/model_study/report.py:468` — `message()` first line.
  - `scripts/lib/zone.py:147` — `notice()`, sent with `--from zone`.
- **Test lanes:** the `test_*.py` beside each changed script.
- **Build:** none.
- **Test:** `~/.claude/scripts/lib/py ~/.claude/scripts/production/targeted_test.py --repo <worktree>` (`--module <suite/module>` while iterating).
- **Lint:** `basedpyright` on each changed Python file: zero errors, zero warnings.
- **Invariants:**
  - `From the user (via ...)` lead-ins stay: they mark the user's words (`docs/production_format.md` → `<ProductionUnit/>` item 2).
  - No script parses a `From <name>:` lead-in, so removing it changes no reader.

## Phases

### Phase 1 — Remove the "From" lead-in from notice templates and script texts  · status: done

#### As-built

- Notice templates start with their content: the checkpoint and shrink notices (`docs/production_format.md`), the no-change ticket notice, the ETA notice (`commands/unit/eta.md`) and the add-ons notice (`commands/unit/add_ons.md`).
- `style_sync.sync_notice()` sends `branch point <b> moved to <sha7>; sync.`.
- `report.message()` starts with `<INTERIM|FINAL> director model study — <verdict>.`.
- `zone.notice()` starts with `The user's time zone is now <zone> (<label>, <offset>) as of <HH:MM> <label>; it was <before>.`.
- `From the user (via ...)` lead-ins stay.

**Files:**
- `scripts/style/style_sync.py`, `scripts/model_study/report.py`, `scripts/lib/zone.py` — notice texts

**Ruled out:** a converter for a `/zone` notice queued before the change; the queue was empty.

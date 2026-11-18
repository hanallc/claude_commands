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

#### Work Order

**Goal:** No notice template or script message starts with `From <unit>:`, `From <caller>:`, `From model-study:` or `From /zone (...)`. `From the user (via ...)` stays.

**Spec:**

The user (via natedev, 2026-11-18) asked for shorter message lines. Postmaster now shows `<sender>: <text>`, so a `From <name>:` lead-in repeats the sender. natedev told all units to stop using the lead-in and approved this change.

1. **Templates.** Remove the `From <unit>: ` (or `From <caller>: `) prefix and keep the rest of each text:
   - `docs/production_format.md:275` (checkpoint notice) and `:288` (shrink notice). Also the prose near `:290` that says the notice "ends" a certain way stays correct.
   - `docs/direct/style_unit.md:127`, `docs/style_production.md:684`, `docs/as-built/style-showrunner.md:603` — `ticket <id> closed with no code change; <n> held.`
   - `docs/as-built/style-showrunner.md:610` — `branch point <b> moved to <sha7>; sync.`
   - `docs/as-built/showrunner-automation.md:213` — the shrink notice.
   - `commands/unit/eta.md:37` — `Phase <N> ETA: …`.
   - `commands/unit/add_ons.md:76` — `<n> add-ons for your call.`
   - If a sentence near a changed template names the lead-in (for example "starts with `From <unit>:`"), rewrite it to match.
2. **Script texts.**
   - `scripts/style/style_sync.py:143`: `f"branch point {branch} moved to {tip[:7]}; sync."`.
   - `scripts/model_study/report.py:468`: first line `f"{kind} director model study — {verdict(...)}."`. Update `docs/as-built/director-model-study.md:60` to match.
   - `scripts/lib/zone.py:147` `notice()`: start with `f"The user's time zone is now {zone} ({label}, {_offset(now)}) as of {now:%H:%M} {label}; it was {before}. "` and keep the rest of the text.
3. **Tests.** Change the expected rows: `scripts/style/test_style_sync.py:722,811`, `scripts/model_study/test_report.py:433,452,511`, `scripts/model_study/test_rerun.py:595`, and `scripts/lib/test_zone.py` where it checks the notice text.
4. **Search.** After the edits, `git grep -nE "From <[a-z -]+>[:( ]|From \{[a-z_]+\}:|From model-study|From /zone|From style_sync"` outside `docs/plans/` returns nothing.

**Files:**
- `scripts/style/style_sync.py`, `scripts/style/test_style_sync.py` — sync notice
- `scripts/model_study/report.py`, `scripts/model_study/test_report.py`, `scripts/model_study/test_rerun.py` — study message first line
- `scripts/lib/zone.py`, `scripts/lib/test_zone.py` — zone notice
- `docs/production_format.md`, `docs/direct/style_unit.md`, `docs/style_production.md`, `commands/unit/eta.md`, `commands/unit/add_ons.md` — templates
- `docs/as-built/style-showrunner.md`, `docs/as-built/showrunner-automation.md`, `docs/as-built/director-model-study.md` — as-built texts

**Workers:** 1 writer — `impl` holds every file; the change is small and each test row changes with its code.

**State:** none

**Constraints from prior phases:** none

**Acceptance gate:**
- `targeted_test.py --repo <worktree>` passes, except a red that also fails on the base commit.
- basedpyright reports zero errors and zero warnings on each changed Python file.
- The Spec item 4 search returns nothing.

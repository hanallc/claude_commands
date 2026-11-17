# sdlc-phases

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready; long-running.** Own the phase process: measure how a `/unit:direct` phase runs from start to end and how design and documentation docs are made before a plan exists, find where time and tokens go, propose changes, then build and measure one approved change at a time.

> **Production: sdlc** — unit `phases-unit`; production doc `docs/plans/sdlc-production.md`

## Source

2026-10-20 16:47 AST

I think we do need at least a unit director devoted in Nate Dev to managing the phase process.

how we prepare our documentation and design and how the design and documentation process runs

For active process redesigns, as we are currently undergoing with promotions and unit director renames and responsibility changes and process changes, those should come back to me as questions.

## What exists today (measured 2026-10-20 16:45–16:51 AST by the plan author)

- **Phase process files:** `commands/unit/direct.md` (1579 lines), `docs/delegate/*.md` (8 files, 1185 lines), `commands/plan/*.md` (4 files, 1824 lines: `to_phased_plan`, `phase_review`, `shrink`, `to_as_built`), `commands/unit/*.md` helpers (`brief`, `checkpoint`, `ci`, `phase_report`, `eta`, `add_ons`, `report`, `style_review`), `docs/delegate_plan_format.md` (255 lines), `docs/production_format.md` (468 lines). Since then `docs/delegate/` moved to `docs/direct/` and `docs/delegate_plan_format.md` to `docs/direct_plan_format.md`.
- **Before a plan exists:** `commands/team_review.md`, `adversarial.md`, `api_review.md`, `module_review.md`, `astra_review.md`, `adhoc_review.md`, `commands/producer/greenlight.md` and `to_production.md` (319 lines), `commands/showrunner/proposal.md`. No repository holds a `*-design.md` doc today; hierarchies-unit starts the first one.
- **Agent time** (`history.py summary`, all 206 run files, 1777 phases): fix 40%, implementation 35%, test 17%, review 5%, other 2%, final (shrink, closeout, phase review, synthesis) 1%; 4564 h of agent time over 701 h elapsed. 197 phases (11%) with 4 or more fix passes hold 75% of fix time. The unit director's own time between passes (compose, synthesize, checkpoint, shrink, phase review) is not a stage in `history.py`.

## Decisions

- **Every change to the phase process is a question for the user** (user, 2026-10-20). This unit measures and proposes; the showrunner brings each proposal to the user; no live command, doc or script that runs phases changes before the user approves it. Measuring and writing the design doc need no approval (plan author).
- **Owner of the phase process files** (production doc, Hub files): this unit owns `commands/unit/direct.md`. guilds-unit (designer and reviewer roles) and fix-rounds-unit (repair round rules) send this unit their edits to it, and this unit merges them into one coherent contract (plan author, from the hub table). It is also a build-followups hub file: an edit goes through cargo-berth's normal flow and the checkpoint notice names the build-followups owner.
- **Scope beside the other units** (production doc, Scope of each unit): this unit owns the steps of a phase and the doc pipeline before a plan. fix-rounds-unit owns repair round counts and their rules; this unit's measure reports fix time as one number and does not study it. guilds-unit owns who designs and who reviews; this unit owns which docs exist, their formats and the commands that turn one into the next. roles-unit owns responsibilities and names; a step that changes either goes to roles-unit through the showrunner. hierarchies-unit (build-followups) owns the structure of productions.
- **One design doc** holds the map, the baseline, the friction, the candidates and the steps: `docs/plans/sdlc-phases-design.md` (plan author). It becomes the as-built at run end.
- **One measure script, run again after each step** (plan author): `scripts/direct/phase_process.py`. It sits in `scripts/direct`, which the merge gate and `basedpyright scripts/direct` already cover, so it needs no new suite or pyright entry. A step counts as done only when its measure moved.

## Delegation Context

- **Project:** `~/.claude` — Claude Code commands, skills, hooks and scripts that run the user's productions. This plan measures the phase process and the doc pipeline before it, and changes them one approved step at a time.
- **Project started:** 2026-10-20T20:53:27.319+00:00
- **Stack:** Python 3.13, standard library only; Markdown.
- **Layout:**
  - `scripts/direct/phase_process.py`, `scripts/direct/test_phase_process.py` — the measure script and its tests (new)
  - `docs/plans/sdlc-phases-design.md` — the design doc (new)
  - read only, outside the repository: `~/.local/state/plan-delegate/runs/*.jsonl` (phase, pass and activity events), `~/.local/state/*/runs/*.jsonl` (other skill stores `history.py` reads), the usage records `scripts/usage/collect.py` reads, `/tmp/claude/unit/<run-id>/` session dirs (findings ledgers, prompts) where still present, `~/rust/hanadocs/issues` (proposals), git history of every worktree's `docs/plans/` and `docs/as-built/`
- **Key files:**
  - `commands/unit/direct.md` and `docs/direct/*.md` — the phase contract: every step a unit director runs
  - `commands/plan/to_phased_plan.md`, `phase_review.md`, `shrink.md`, `to_as_built.md` — plan compile, fold-in, shrink, as-built
  - `commands/unit/checkpoint.md`, `brief.md`, `phase_report.md`, `ci.md` — phase helpers
  - `docs/direct_plan_format.md` — the plan format; `docs/production_format.md` — <ProductionUnit/>
  - `commands/team_review.md`, `adversarial.md`, `api_review.md`, `module_review.md`, `astra_review.md`, `commands/producer/greenlight.md`, `to_production.md`, `commands/showrunner/proposal.md` — the doc pipeline before a plan
  - `scripts/history/history.py` — stage timings; its `PASS_STAGE`, `ACTIVITY_STAGE` and `ACTIVITY_ALIASES` map labels to stages
  - `scripts/direct/progress_history.py` — the recorder and its event model (`docs/as-built/plan-delegate-progress-history.md`)
  - `scripts/direct/findings.py` — the findings ledger
  - `scripts/usage/usage_report.py`, `collect.py` — tokens by session
  - `scripts/lib/user_zone.py` — user-facing times
  - `~/.claude/docs/decision_criteria.md` — what reaches the user
- **Test lanes:** `scripts/direct/` — `test_*.py` beside the script.
- **Test:** `python3 -m unittest discover -s scripts/direct -p 'test_phase_process.py'`, from the worktree root. Gate: `env HERDR_BIN=/nonexistent/herdr python3 scripts/production/merge_gate.py --repo . --tests-only` from the worktree root (memory `shared-module-full-gate`).
- **Lint:** `basedpyright scripts/direct scripts/production` passes when its output has the `0 errors, 0 warnings` line; the exit status says nothing.
- **Invariants:**
  - Measuring reads every source read-only. It sends no message, push or alert, and starts no session (plan author: a measure must not add traffic).
  - Tests use fixtures; they never read the real `~/.local/state`, `/tmp/claude`, `~/rust/hanadocs` or transcripts.
  - Each number states its window and its count; medians and p90 come with n.
  - Python is typed throughout, with no `Any` and no file-level type ignores (user, `~/.claude/CLAUDE.md`).
  - User-facing times use `scripts/lib/user_zone.py` (America/New_York, user 2026-10-12); log stamps stay UTC.
  - All text uses Simplified Technical English (user, 2026-10-18).
  - No step changes a live phase process file before the user approves it (user, 2026-10-20).

## Phases

### Phase 1 — One command measures each step of a phase and the doc pipeline before a plan, and the design doc maps the process  · status: done
**Depends on:** none

#### As-built

- `scripts/direct/phase_process.py baseline [--since DATE] [--until DATE] [--detail TABLE]` reads run records, usage.db and git. It writes no store of its own. The default window is the last 14 days.
- It prints five tables. Phase steps: wall time per step kind and activity, split equally where steps overlap; director time is the time no step covers; also the gap from phase end to the next phase start. Director tokens per phase, with the share before the first worker step and after the last. Contract load: each "Read … in full" file, followed one level out, with lines, bytes and cadence. Doc pipeline: first doc commit, first `run_started` and first `phase_started`, per doc kind and command. Plan churn: Work Order edits, decisions, and phases inserted after the run starts.
- Each table prints the top 10 rows and a summed `other` row, the window once in its title, and n on every number. `--detail TABLE` prints every row. A missing source prints `not recorded`. A source the Mac also keeps prints `natedev only`.
- Fix time is one stub row that names `history.py rounds` as its source.
- The design doc holds Map (22 phase-step rows and 7 doc-kind rows, each with its defining file or tag and its baseline row), Baseline (verbatim output for 2026-10-06 to 2026-10-20), Friction (measured and reported lines, each with a source) and Questions for the user (one).

**Files:**
- `scripts/direct/phase_process.py` — the measure command; read-only.
- `scripts/direct/test_phase_process.py` — 21 fixture tests.
- `docs/plans/sdlc-phases-design.md` — Map, Baseline, Friction, Questions.

**Binds later work:** Phase boundaries and fix time each sit behind one function in `phase_process.py`. The rounds.py swap replaces those two functions and the stub row, with fixture tests. One run record can hold more than one plan phase, so per-phase counts change after the swap. Candidate ranking reads the design doc's Map, Baseline, Friction and Questions sections.

**Gotchas:**
- Git history runs in the repository that holds each doc. A plan can name paths in other worktrees.
- Source docs come from bare paths and Markdown links in each header version, unioned.
- An open review start makes the after-last-step token share `not recorded` for that phase.

**Ruled out:** a second store of run data (the script reads the same sources as `history.py rounds`); taking the token-versus-dispatch-time question to the user alone (rank candidates both ways side by side, with one question at the end).

### Phase 2 — The design doc recommends changes to the phase process and the doc pipeline, split into steps  · status: done
**Depends on:** Phase 1

#### As-built

- `phase_process.py` reads phases and fix time from `scripts/history/rounds.py`. An open phase is a named state, never epoch 0. A completed phase interval and fix worker time are distinct types. Fix time prints as worker time, apart from the wall-time shares. A mixed phase (one run record with more than one plan phase) is out of per-phase step costs and medians and is reported apart with its n.
- `doc after dispatch` is a separate doc-pipeline state: a doc whose first commit comes after its plan's first dispatch. It is counted apart with its n, listed under `--detail doc-pipeline`, and kept out of lead-time distributions, totals and cost ranking.
- `sdlc-phases-design.md` has Candidates, Comparison (one ranking by director tokens first, one by dispatch time first), Recommendation and Steps. Its Baseline is the `phase_process.py baseline` output after the rounds.py swap.
- The design doc records the user decision of 2026-10-28: director tokens come first. Step 1 is cheap A: `commands/unit/direct.md` becomes a short route, and each step reads only its own rule file. Cheap A targets a 3–7% token cut and a 0–3 s dispatch-time cut. Candidate C is not built now.

**Files:**
- `scripts/direct/phase_process.py` — the measure script: rounds.py phases and fix time, the `doc after dispatch` state
- `scripts/direct/test_phase_process.py` — fixtures for parallel repairs (worker time above wall time), open phase, mixed phase, ledger-only round, closure review, doc after dispatch
- `docs/plans/sdlc-phases-design.md` — map, baseline, friction, candidates, comparison, recommendation, steps

**Binds later work:** `read_usage` in `phase_process.py` sums input and all cache classes into one number and drops the model, so weighted tokens need a different read. `phase_boundaries` selects phases by finish time, and `measure_director_tokens` stops at phase end. Phase boundaries and fix time come from `scripts/history/rounds.py`: an open phase is a named state; fix time is worker time, apart from wall-time shares; a mixed phase is reported apart. `direct.md` costs about 41k tokens as a Read. `phase_process.py` has director tokens per phase, but no compactions per phase and no context size after reload.

**Gotchas:** closure reviews remain review passes.
A dispatch without an eligible fix pass adds no repair round.
Started passes with one ledger batch count once across relaunches.
Legacy passes without a ledger number group by overlap.
A repair with `cause == "merge"` counts under fix, not under the merge row.
The saved baseline reports inserted phases as about 2% of gaps.

**Ruled out:** a retrospective before sample for cheap A — a phase's start time does not prove which contract it read; the full A targets (10% tokens, 10 s) — the measured cut does not reach them.

### Phase 3 — The recorder records which phase contract each phase read, and the trial's three measures  · status: done
**Depends on:** Phase 2

#### As-built

- `progress_history.py start-phase` and `finish-phase` add a `contract_revision` field to their event: the `~/.claude` main HEAD commit, the sha256 of `commands/unit/direct.md` there, and the sha256 of each file its `<TagReferenceContract>` table names. A dirty file is hashed as it is on disk. A missing file gets a named missing state, never an empty hash. `direct.md` itself does not change. Readers accept an old event without the field.
- `phase_process.py baseline` gives each trial phase a `TrialExposure`: `BeforeChange`, `AfterChange` or `ExposureNotRecorded(reason)`. A phase is `BeforeChange` or `AfterChange` only when its start and finish records name the same `direct.md` hash and that hash is on one side: at or before `--before-at <commit>`, or on the route side of `--route-revision <commit>`. A hash on both sides or on no side, no record, or two different hashes give `ExposureNotRecorded`. No deploy time stands for exposure.
- `select_trial_phases` selects trial phases by start time and adds the following gap. A phase plus its gap is eligible only when the next phase in the same run starts with the same `direct.md` hash. `NoFollowingPhase` and `SharedDirector` (two phases open at once with one director) are counted apart with n, before the exposure filter. `phase_boundaries` keeps finish-time selection.
- Three measures per eligible phase, each with n. Weighted director tokens over the phase plus its gap is the primary measure: `read_usage` keeps token classes and the model apart, weights come from `scripts/usage/weights.py`, a model with no weight is a named unweighted state, and raw input and output print apart. Compactions per phase count `compact_boundary` entries. Context size after each reload is the raw input of the first request after the first contract Read that follows a boundary. `NoCompaction`, `NoReload` and `NotRecorded(reason)` are named states, never 0.
- Filters are `--units <a,b>`, `--exposure before|after` and `--detail trial`; windows are `[since, until)` in the user zone. Output starts with a 3-line summary. Repair medians use repaired phases only. Durations over 1000 s print in minutes.
- `tag_inventory.py` checks that each `<Tag/>` reference in `/unit:direct`'s resolved contract has exactly one body. Scope: `commands/unit/direct.md`, each file its `<TagReferenceContract>` table names, and the `docs/` Markdown links of those files, recursively. Fenced code, quoted examples, plan docs and other commands' own tags are out of scope. It fails on a missing body, two bodies, a stale path and a forwarding cycle.

**Files:**
- `scripts/direct/progress_history.py`, `scripts/direct/test_progress_history.py` — `contract_revision` on phase events
- `scripts/direct/phase_process.py`, `scripts/direct/test_phase_process.py` — `TrialExposure`, eligibility, the three measures, the filters
- `scripts/direct/tag_inventory.py`, `scripts/direct/test_tag_inventory.py` — the route's structure check
- `commands/unit/checkpoint.md` — names `<RetainPhaseReservation/>`
- `docs/plans/sdlc-phases-design.md` — full A is 10-16 h; cheap A is Step 1; the before sample is prospective from the recording's deploy, and its freeze uses `--before-at`

**Binds later work:** the short route freezes the before sample with `phase_process.py baseline --exposure before --before-at <~/.claude main HEAD at the freeze> --units <units>`, moves rule bodies as they are, and must keep `tag_inventory.py` green. The 20-phase trial measures the after side with `--route-revision <route commit>`. Wait records and merge receipts sit beside `contract_revision` in the same run file, and `scripts/history` readers accept old and new events.

**Gotchas:** `test_phase_process.py` holds 3 tests that read the design doc's Recommendation, Comparison and Steps; an edit there must keep them green. Since Phase 14, `phase_table.py` reads a `**Depends on:**` line as phase ids only from `Phase <id>` spans of this plan; other conditions go on a `**Waits for:**` line. `scripts/guilds/daily_read.py` needs finish-time selection from `phase_boundaries`.

**Ruled out:** a retrospective before sample — the design's Steps need a prospective one; start-time selection inside `phase_boundaries` — it breaks `daily_read.py`; deploy time as exposure — it does not prove which contract a phase read.

### Phase 4 — Freeze the before sample for the `direct.md` route that shipped  · status: done

**Depends on:** Phase 3, Phase 6, Phase 7, Phase 8, Phase 9, Phase 11, Phase 12, Phase 13, Phase 14, Phase 15, Phase 16, Phase 17, Phase 18, Phase 19, Phase 20

#### As-built

- The design doc's `## Step log` holds the frozen before sample for the route that shipped as fix-rounds-unit phase 10c (c74d9d50e; main 7733bee4cdb9d75cd9680482809eff05c1a788e3, 2026-11-07 23:32:53Z): `direct.md` is a 45 KB core, and twelve groups of sections are in `docs/direct/`.
- Window: phase starts in `[2026-11-03T18:17:52Z, 2026-11-07T23:32:53Z)`, from the staff-lifecycle cut's main time to the route's main time. One `phase_process.py baseline --exposure before --route-revision 7733bee4c… --units <unit>` invocation per unit (18 in all); the before anchor is the route's parent e1d8b73ed.
- Sample: 31 eligible instances from 9 units (cargo-liner-handler, claude-natedev-builds-natest, claude-natedev-builds-resources, claude-natedev-efficiency, claude-natedev-unit-board, devtools, hana_catalyst-build-speed-tests, remove-running-section-handler, sdlc-devtools). Each unit has its own measure summary, from its own `--detail trial` rows where a selector also picks another unit's runs.
- Each row records run, display phase, instance, start, exact weighted tokens (`str(row.tokens.weighted)`), and the matching fields with their source: Mode `NotRecorded` for every row; scope class from the retained Work Order's `Files` list; worker model from `pass_started.called_agent`; gates from the retained Work Order or prompt.

**Files:**
- `docs/plans/sdlc-phases-design.md` — `## Step log`: window, route revision, invocations, per-unit samples, matching fields

**Binds later work:** deployed revisions (first ~/.claude main commit holding each checkpoint, EDT, read 2026-10-30): Phase 3 b0b19884 2026-10-29 05:35; Phases 6 and 7 f65fb53f 13:40; Phases 8, 9 and 12 bbe7f72a 16:54; Phase 11 18998c02 17:30; Phase 13 b3baf668 19:26; Phase 15 f736b486 20:14; Phase 16 adf8f23b 21:02; Phase 14 5654117c 22:01; Phase 18 ef22f72a 22:19; Phase 19 b1e7c304 22:52. Phases 17 and 20 never reach main: the staff-lifecycle cut cd341ee71 deleted their code and `publish_wait`; it is on main at 3fa44fde1 (2026-11-03 13:17:52 EST). postmaster-unit's write-once reply records (dfb1ef64f) are on main at 87764c7a1 (2026-10-29 14:37:05 EDT). The phase that measures the first step reads this Step log.

**Gotchas:** `--units` matches run-start unit, worktree, branch and working-directory name, so selector totals can count another unit's phases. Detail rows omit the instance id; a display id with several starts in the window is `Ambiguous`. builds-resources scope and gates cite the Work Order at each phase's checkpoint commit; the checkpoint parents lack it. The devtools transcript has invalid JSON at line 13452, so its compaction counts are unavailable.

**Ruled out:** mode recording (showrunner, 2026-11-08); `--before-at` for this sample (`--route-revision` reads the before hashes from the route's parent).

### Phase 5 — Measure the first step and propose the next  · status: done
**Depends on:** Phase 4

#### As-built

- The Step log's "After sample (Phase 5)" section holds the cheap A after measure: window `[2026-11-07T23:32:53Z, 2026-11-15T14:00:00Z)`, run at the before sample's measurement version (archived from `96c882a1e`), one summary and one `--detail trial` run per unit. A second run matched line for line.
- 25 eligible after phases, 24 with known weighted tokens, from five units present in both windows. The pooled median rose 3.06% (n=9 before, n=24 after). Phase time rose in four of five units.
- Result: `inconclusive`. Every after worker pass records model `unknown`: `progress_history.begin_dispatch` writes `unknown` for each pane dispatch (since `a453a2ca4`, 2026-11-05). No-worker mode from 2026-11-13 moved implementation into director tokens.
- The Step log lists each main change to the recorder, launchers, hooks, wait publishers and `phase_process.py` inside the after window (95), and to `docs/direct/*.md` and `phase_reservation.py` inside either window (40).
- Steps item 1 notes the result. sdlc decided (2026-11-15): keep cheap A, fix the worker-model recording now (Phase 29), and measure again after the workers-back notice (Phase 30).

**Files:**
- `docs/plans/sdlc-phases-design.md` — Step log, Steps

### Phase 29 — Each worker pass records its model  · status: done
**Depends on:** Phase 5

#### As-built

- `progress_history.begin_dispatch` takes `called: CalledAgent` (family, model, effort). The default `CALLED_AGENT_NOT_RECORDED` is `unknown / unknown / unset`, so test calls in other modules keep their form.
- `worker.launched_agent(row)` reads the worker's newest pane launch record (`pane_record.launch_for_session`). The family is the launch kind; the model and effort come from `worker_panes.kept_settings` over `pane_record.resume_arguments`. A Claude launch keeps no effort and records `unset`. No record or no kept model records `unknown / unknown / unset`. The herdr dispatch passes `called=launched_agent(started.row)`.
- Tests: `test_dispatch_records_the_called_worker_model` (test_progress_history.py) and `test_launched_agent_reads_the_kept_launch_settings` (test_worker.py).
- A live read of 607 worker rows resolves the 30 that have launch records. A live dispatch proof waits for the workers-back notice.
- The Step log's "Worker model and NoReload cause (Phase 29)" section gives the NoReload cause. In 86 cargo-liner-handler boundaries of the after window, 27 reload with `Read`, 21 reload with Bash only, and 38 do not reload. 71 of 86 show the skill text that Claude Code restores after compaction. The measure counts only `Read`.

**Files:**
- `scripts/direct/progress_history.py` — `CalledAgent`, `begin_dispatch`
- `scripts/production/worker.py` — `launched_agent`, the `begin_dispatch` call
- `scripts/direct/test_progress_history.py`, `scripts/production/test_worker.py`
- `docs/plans/sdlc-phases-design.md` — Step log

**Gotchas:** `pane_launch.CurrentLaunch["kind"]` is not a required key; read it through `pane_launch.saved_kind`.

### Phase 30 — Measure cheap A again on worker-mode phases  · status: todo
**Depends on:** Phase 29

**Waits for:** (the workers-back notice is met: 2026-11-15 13:36 EST) at least 20 eligible `AfterChange` phases with a recorded worker model from at least two before-sample units, after Phase 29 reaches main.

#### Work Order

**Goal:** the design doc says whether cheap A moved director tokens on worker-mode phases, and the next step goes to the showrunner.

**Spec:**
- Run the frozen protocol of Phase 5. Fixed `--since` (Phase 29's main time) and `--until` (the collection time) as UTC stamps; one invocation per unit, summary and `--detail trial`; the measurement version is blob `98bef1370ee83586a43f1b0e12cd16b92b30876e` from `git archive 96c882a1e`; `--exposure after --route-revision 7733bee4cdb9d75cd9680482809eff05c1a788e3`. Record each after instance and its matching metadata (scope class, worker model, gates) as Phase 5 did.
- Measure: weighted director tokens over the phase plus its following gap. Target: the pooled median and the median of each matched unit 3–7% lower than the Step log's before sample; a fall below 1.5% is a proposal to undo or change cheap A. n counts phases with known weighted tokens. Report compactions per phase and context size after reload with no target, and median and p90 phase time per unit.
- Report each excluded state with n: `ExposureNotRecorded`, `NoFollowingPhase`, `SharedDirector`, mixed, open unmixed, `Unweighted`, `NotRecorded`, `NoCompaction` and `NoReload`.
- Matching: a phase with no worker pass, or with a worker model `NotRecorded`, does not match a before row with a recorded worker model. A control `NotRecorded` in either window makes the result `inconclusive`. Mode stays a named `NotRecorded` limit.
- Start no new step (B, full A, G or I) before this result (showrunner, 2026-11-15). Send the showrunner the result and the next step.

**Files:**
- `docs/plans/sdlc-phases-design.md` — Step log, Steps

**Workers:** director only — the director runs the measure and writes the Step log.

**Acceptance gate:** the Step log has the after numbers with n and window, the matching result and the phase-time check; `test_phase_process.py` passes; the result is sent.

### Phase 31 — Every read-only Claude run has no file-edit tools  · status: done
**Depends on:** none

#### As-built

- `scripts/agents/agent_exec.sh` readonly Claude mode passes `--permission-mode plan --disallowedTools Edit,Write,NotebookEdit`. Write mode keeps every tool.
- The `direct.simplify`-only tool branch and its readonly-only guard are deleted. The one caller, `simplify_daily.py`, runs readonly.
- `test_agent_exec.sh` checks the exact readonly argv for a plain task and a tier task, and the removed tools for simplify.

**Files:**
- `scripts/agents/agent_exec.sh`
- `scripts/agents/test_agent_exec.sh`

**Gotchas:** the dry run prints each argument with `%q`, so the expected argv has `Edit\,Write\,NotebookEdit`.

### Phase 32 — One place decides a Claude run failed, and one helper reads the review minutes  · status: done
**Depends on:** none

#### As-built

- `scripts/direct/simplify_result.py` `validate` reads only `stats` from `claude_result`. A failed Claude run reaches it as the `ERROR: <reason>` report that `agent_exec.sh` writes through `claude_result.py`; that is the one failure check.
- `scripts/production/review_regime.py` `optional_float` reads `review_minutes`, `ux_check_minutes` and `ux_repair_minutes`. Values and missing values are the same as before.
- Tests: `test_a_failed_run_records_the_error_from_the_launcher_report` (test_simplify_result.py) and `test_each_review_minute_field_reads_numbers_and_skips_other_values` (test_review_regime.py).

**Files:**
- `scripts/direct/simplify_result.py`, `scripts/direct/test_simplify_result.py`
- `scripts/production/review_regime.py`, `scripts/production/test_review_regime.py`

### Phase 33 — A repair and a checkpoint of the same phase never lose the release record  · status: done
**Depends on:** none

#### As-built

- `phase_reservation.phase_lock(phase_dir)` holds `phase_baseline.lock`, the lock file the baseline writer already used. `committed`, the repair record (`_repair_record`), `record_active`, `enroll`, `clear`, `reset_for_start` and `_record_baselines` hold it across their read and write.
- `release_held` takes no lock itself; its final `clear` takes the lock and checks the pending record again.
- The repair holds the lock during its board check, so a checkpoint can wait up to that check's timeout.
- Test: `test_a_checkpoint_during_a_repair_keeps_its_pending_release_record` fails with the lock removed and passes with it.

**Files:**
- `scripts/direct/phase_reservation.py`
- `scripts/direct/test_phase_reservation.py`

**Gotchas:** flock locks an open file description, so a nested `phase_lock` on the same phase in one process blocks itself. `clear` returns before the lock when no state file exists, so it creates no directory.

### Phase 34 — `remaining` and `ahead` live in note_file.py  · status: done
**Depends on:** none

#### As-built

- `note_file.ahead(resets, *, now)` and `note_file.remaining(note)` hold the two note helpers. `remaining` takes `note_file.FieldReader`.
- `quota_alert` calls `note_file.ahead` and `note_file.remaining`. It keeps no copy and no re-export, so a caller of the helpers does not load `quota_alert`.
- `test_agent_notes.py` reads `note_file.ahead`; no other caller was found.

**Files:**
- `scripts/whoami/note_file.py` — `ahead`, `remaining`
- `scripts/whoami/quota_alert.py` — the call sites
- `scripts/whoami/test_agent_notes.py` — the `ahead` tests

### Phase 35 — The launch account for a Claude task lives in models.conf  · status: done
**Depends on:** none

#### As-built

- `agents_resolve` sets `AGENT_CLAUDE_ACCOUNT` for a claude task from `[<function>.options]`: `claude_account.<subtask>`, then `claude_account`. The value is `workers` (the default) or `caller`; another value stops with `ERROR:` and the allowed values. A codex task gets an empty value.
- `agent_exec.sh` runs a `caller` task on the caller's own account and every other Claude task on the workers pool. No task-name test is left.
- `config/models.conf` has `[direct.options] claude_account.simplify=caller`.

**Files:**
- `scripts/agents/models_config.sh` — `_agents_resolve_claude_account`
- `scripts/agents/agent_exec.sh` — the account branch
- `config/models.conf`, `docs/as-built/agent-registry.md` — the key and its description
- `scripts/agents/test_models_config.sh`, `scripts/agents/test_agent_exec.sh` — each value, and `account_case` on the caller account
- `scripts/lib/test_nix_env_launches.py` — the `agents_resolve` stub sets `AGENT_CLAUDE_ACCOUNT`

**Gotchas:** `agent_exec.sh` runs under `set -u`, so an `agents_resolve` stub must set every variable the launcher reads.

### Phase 36 — The simplify-follow items still in the tree  · status: done
**Depends on:** none

#### As-built

- `agent_exec.sh` calls `claude_result.py` at its one call site. The wrapper function is gone; its comment sits above the Claude command line.
- `simplify_result.py` lists `SIMPLIFY_AGENTS` with `", "`.
- `account_for_launch` in `add_unit.py` calls `accounts.prepare_claude_launch`; its `try` turns `AccountsError` into `Refusal`.
- `account_test_support.store_usage(tool, login, usage)` writes one login's readings under the store lock. `stored_room` and `test_accounts.py` `reading` call it.
- `test_agent_exec.py`, `test_review.py` (two sites) and `add_showrunner_test_support.py` call `create_claude_account`.
- `agent-registry.md` says every readonly Claude run disables the editing tools.

**Files:**
- `scripts/agents/agent_exec.sh`, `scripts/direct/simplify_result.py`, `scripts/production/add_unit.py`
- `scripts/whoami/account_test_support.py` — `store_usage`
- `scripts/agents/test_agent_exec.py`, `scripts/notify/test_review.py`, `scripts/production/add_showrunner_test_support.py`, `scripts/accounts/test_accounts.py`, `scripts/direct/test_simplify_result.py`
- `docs/as-built/agent-registry.md`

**Gotchas:** A long `TMPDIR` makes socket tests fail with `AF_UNIX path too long`; run them with a short `TMPDIR`.

### Phase 37 — An Images card can show a before and after pair  · status: done
**Depends on:** none

#### As-built

User request 11-15 10:24: before and after shots side by side, with one metadata block and one Delete. The user accepted the mockup.

- `images.py register (--image PATH | --before PATH --after PATH)`: a pair is one folder with `before<suffix>` and `after<suffix>`, one record, one id and one image link. The record keeps the after image in `file` and the before image in `before`.
- A pair card (`article.pair`) shows Before and After side by side in `.image-pair`, each linked to its full size. It has one caption, one details block, one Feedback field, one Send feedback and one Delete, which removes the folder.
- The feedback message has a `Before:` and an `After:` link for a pair.
- On a page 1104px or wider, a single card is 680px and a pair card is up to 1376px, both centered. A pair card under 600px stacks its images, before on top (container query).
- `/show_image`, `docs/as-built/review-links.md` and the proof rule in `docs/production_format.md` say: file before and after shots as one pair.

**Files:**
- `scripts/review_links/images.py` — `register(before=)`, `shown_files`, `picture`, the pair card, the CSS and the `register` options
- `scripts/review_links/test_images.py` — `ImagePairTests` and the pair server test
- `commands/show_image.md`, `docs/as-built/review-links.md`, `docs/production_format.md`

**Gotchas:** The branch carries sdlc-fix-rounds at 304c80d16 (merge 6dbb94f81), so it lands after the admin-console batch.

### Phase 38 — A change-review write holds the lock under 1 s, and one batch writes many rows  · status: done
**Depends on:** none

#### As-built

- `_commit(paths, what)` in `change_review.py` commits one or more lists of one repository by plumbing only. It runs from the repository top level: `hash-object -w --path` for each list, then `read-tree` of HEAD (or `--empty`), `update-index --cacheinfo` and `write-tree` in a temporary `GIT_INDEX_FILE`. It sets each list's entry in the real index, then runs `commit-tree` and `update-ref HEAD <new> <old HEAD>`. A moved HEAD retries up to 5 times. A tree equal to HEAD's makes no commit. No command refreshes the index, so clean filters on other modified files never run. A write in a repository shaped like hanadocs takes 0.01-0.06 s, down from about 3.1 s.
- A failure warns "written but not committed: <reason>; the next write commits it" and moves no ref.
- `change_review.py batch --file <json>` accepts an array of `edit` and `verdict` items.
  Each item requires `op`, `production` and `row`; `list` is optional.
  Edit items accept `change`, `check`, `see`, `image` and `output`.
  Verdict items require `verdict`.
  A showrunner check also requires `validation` with the ID from its validation message.
  The command checks item fields and registers images before it takes the locks.
  All lists must share one folder; the command refuses a repeated row.
  It takes the delivery lock before the list lock.
  It checks exchange IDs and stages each verdict while it holds both locks.
  Every item must pass before the command writes a list.
  One commit includes every target list, with subject `change-review: batch <N> writes (<owner> <count>, ...)`.
  An error exits 1 and names the item index.
  A rerun after a stopped batch completes the remaining writes and commits every list.
- `edit_row` and `record_verdict` share `_edit_write` and `_verdict_write` (a `RowWrite` with `write` and `after`) with `batch`.
- `add`, `edit` and batch `edit` items accept `--output` like the `image` action; `--image` with `--output` is refused.
- `show --row <id>` prints that row with `describe`; a row not on the list exits 1.
- `ACTION_OPTIONS` maps each action to the options it reads. Any other option given a value exits 1 and names the option.

**Files:**
- `scripts/production/change_review.py` — `_commit`, `_edit_write`, `_verdict_write`, `_register_image`, `batch`, `ACTION_OPTIONS`, `show --row`
- `scripts/production/test_change_review.py` — plumbing commit, race, unborn HEAD, filter, batch and option tests
- `commands/change_review.md` — `batch`, the plumbing commit, the option rules

**Gotchas:**
- `hash-object --path` takes a path relative to the top level, so it must run from the top level, or list attributes do not match.
- Images a failed batch registered stay unclaimed until `sweep_change_images` removes them.

### Phase 39 — The ten C51 suites pass on the Mac  · status: done
**Depends on:** none

#### As-built

- `scripts/lib/py` writes every array expansion as `${name[@]+"${name[@]}"}` (`shim_parts`, `python_options`, `file_separator`). macOS `/bin/bash` 3.2 runs it under `set -u` with no Python options. Other behavior does not change.
- `scripts.lib.test_py_shim` refuses a bare `"${name[@]}"` expansion in the file (a text check), and runs the shim under `bash -u` with no options (stdin) and with `-c`.
- `NativePaneSessionTests` pins `herdr._PLATFORM` to `"linux"` in the three tests that fake `/proc`: `test_bare_codex_launch_uses_its_process_instead_of_the_tabs_old_thread`, `test_codex_native_process_uses_its_resume_argument_or_own_pane_report` and `test_cached_starting_pane_reports_once_in_each_run`. Product code does not change.
- The account_move preview test sets `CLAUDE_PYTHON=sys.executable`, so `scripts/lib/py` writes no interpreter cache under the test HOME on darwin.
- Proof: a `mac_queue_run.py` run of the ten C51 suites plus `test_py_shim` at 7d40bfcc5 with this diff ran 826 tests OK; natedev ran 844 tests OK.

**Files:**
- `scripts/lib/py` — the Python launcher shim
- `scripts/lib/test_py_shim.py` — shim tests and the bare-expansion check
- `scripts/herdr/test_herdr.py` — `NativePaneSessionTests` platform pin
- `scripts/agents/test_account_move.py` — the preview test sets `CLAUDE_PYTHON`

**Gotchas:**
- Under `set -u`, bash 3.2 treats `"${arr[@]}"` of an empty array as unbound. Bash 4.4 and later do not, so natedev cannot show the failure; the text check stands in for it.
- On darwin, `scripts/lib/py` writes `$HOME/Library/Caches/claude/python3-x.y` unless `CLAUDE_PYTHON` is set. A test that compares HOME disk state sets `CLAUDE_PYTHON`.
- A test that fakes Linux `/proc` pins `herdr._PLATFORM` to `"linux"`; on darwin, herdr reads `ps` instead.

**Ruled out:** a class-wide platform pin in `setUp` (most `NativePaneSessionTests` tests do not fake `/proc`).

### Phase 40 — Every main push lands through one function, and the post-push hook runs after each one  · status: done
**Depends on:** none

#### As-built

- `scripts/lib/land_main.py` holds `land(checkout, commit, branch="main", *, time_limit=UNLIMITED_GIT) -> Landed(commit, branch, push, hook)` and a CLI (`--checkout`, `--commit`, `--branch`). Under `main_push_lock.main_push_lock(checkout)` it fetches the branch, then:
  1. Origin at the commit: `AlreadyLanded`; the hook runs unless the record names the commit.
  2. The commit is a strict ancestor of origin: `AlreadyLanded` with `HookSkipped`. The newer landing owns the hook.
  3. Origin is an ancestor of the commit: a plain fast-forward push (`Pushed`).
  4. Otherwise: `NotFastForward(LandError)`, and no hook runs.
- On the default branch (`refs/remotes/origin/HEAD`, else `main`), it runs `<checkout root>/.claude/config/post_push.sh` with bash, with `LANDED_SHA` and `LANDED_BRANCH`, inside the lock. After exit 0 it writes the commit to `post-push-completed` in the git common dir. An unreadable record counts as no record.
- Hook outcomes are `HookAbsent`, `HookDone`, `HookSkipped` and `HookFailed(exit_code, rerun_command)`; `hook_report(hook)` gives one line. A failed hook never fails the landing: the CLI prints the rerun command and exits 0. It exits 1 on `LandError`.
- Every default-branch push calls it: `push_direct.sh`, `push_pr_branch_and_merge.sh`, `restore_unreleased.sh`, `main_publish.push`, `fast_track`, `merge_checkpoint.promote_local_checkout`, `merge_gate.promote`, `production_lifecycle.promote_main`, `models_registry._publish` and `vault_pull.publish_mac` (120 s time limit). `post_push_hook.sh` no longer exists.
- `main_push_lock.py`'s CLI passes `held_environment(checkout)` to its child, so a nested `land_main.py` reuses the hold.
- Callers report the hook: `main_publish` only when it ran or failed; `merge_checkpoint` as a `post-push` line, `warning` on failure. `commands/showrunner/produce.md` states that each promotion syncs hana's mirror through the hook.
- Hook interface (agreed with hana-merges): cwd is the landing checkout root, which can be a merge-branch worktree; exit 0 means the sync is done; the hook can run under the main push lock, must not push the default branch, and must be safe to rerun for one `LANDED_SHA`.

**Files:**
- `scripts/lib/land_main.py`, `scripts/lib/test_land_main.py` — the landing path and its tests
- `scripts/lib/main_push_lock.py`, `scripts/lib/main_publish.py`, `scripts/lib/models_registry.py`, `scripts/buildlog/vault_pull.py` — callers
- `scripts/production/production_lifecycle.py`, `scripts/production/merge_checkpoint.py`, `scripts/production/merge_gate.py`, `scripts/production/fast_track.py` — promotion callers
- `scripts/validate_and_push/push_direct.sh`, `scripts/validate_and_push/push_pr_branch_and_merge.sh`, `scripts/release/restore_unreleased.sh` — shell callers of the CLI
- `docs/as-built/validate-and-push-cancel-prior.md`, `commands/showrunner/produce.md` — docs

**Binds later work:** the release's main push (`restore_unreleased.sh`) runs `post_push.sh`; `test_release.py` installs a hook file and checks `post-push hook: done`.

**Gotchas:**
- `land` fetches inside the lock even when the caller fetched before; tests that count fetches see two.
- A test that injects a push failure patches `land_main.land` where the caller looks it up, not `git push`.
- `systemd-run` expands `${...}` in its arguments; run a gate loop from a script file.

**Ruled out:** a nonzero exit on a hook failure (the landing is done; the rerun line is the remedy).

### Phase 41 — Each release step skips work that is done, and the GitHub release goes to the configured repository  · status: done
**Depends on:** none

#### As-built

- A `/release` rerun after a partial failure finishes the release. STEP 0 reads the version at `HEAD` from the `Cargo.toml` of `${PACKAGE}`, else of the primary crate. When it equals `${VERSION}`, the run resumes: `${RESUME_FLAG}` is `--resume`, STEPs 2–4 are skipped and STEP 5 checks out the release branch. A resume starts from `${BASE_BRANCH}`, or from the hotfix branch in hotfix mode.
- `validate_version.sh <version> [--resume] <crate>...`: with `--resume`, a published crate prints `<crate>: <version> already published (resume)` and the next crate is checked.
- `create_release_branch.sh`: a release branch at `HEAD` is checked out (`reusing release branch <branch>`); one at another commit is refused with both commits named.
- `publish_crate.sh`: reads the package version with `cargo metadata`; when `verify_published.sh` passes it prints `already published: <crate> <version>` and skips the dry-run and the publish.
- `create_github_release.sh`: `gh release view <tag> --repo <repo>` exit 0 prints `already released: <tag> on <repo>` and exits 0. Before a create it checks `gh api repos/<repo>/git/ref/tags/<tag>`: HTTP 404 prints `ERROR: tag <tag> is not on <repo>; push it there first`; another failure prints gh's error and `ERROR: could not check tag <tag> on <repo>`. Creates use `--verify-tag`; a failed create prints `ERROR: gh release create failed for <tag> on <repo>`.
- STEP 1 takes the GitHub release repository from `github_release_repo` (`owner/name`) in `.claude/config/release.toml`. With no key, origin is used only when `gh repo view --json isPrivate` is false; else STEP 1 stops and names the key. STEP 8 passes that repository and needs the tag there first.

**Files:**
- `commands/release.md` — STEP 0 resume, STEP 1 repository choice, STEP 8 tag rule, `github_release_repo` in Configuration
- `scripts/release/validate_version.sh`, `create_release_branch.sh`, `publish_crate.sh`, `create_github_release.sh` — the skips and refusals
- `scripts/release/test_release.py` — stub `gh`, `cargo` and `curl` on `PATH`; no test publishes or makes a release

**Binds later work:** Phase 42's per-crate pipeline uses these skips; a crate is done when `verify_published.sh` and `gh release view` both pass.

**Gotchas:** `push_release.sh` and `restore_unreleased.sh` were already safe to rerun.

### Phase 42 — One `/release` run releases a list of crates and runs the post-publish hook for each  · status: done
**Depends on:** Phase 40, Phase 41

#### As-built

- `/release <bump|version> <crate>[@<version>] ...` with more than one package runs Multi-Package Mode in `commands/release.md`. One package keeps single-package mode. Multi-package mode runs only in normal and isolated mode, from `${BASE_BRANCH}`: on a `release-` branch STEP 1 stops, and hotfix mode stops.
- STEP 1 orders the crates by `publish_order` (an array of crate names) in `.claude/config/release.toml`; a named crate absent from it stops the run. Without the key, the order is the order given. Each crate's version is resolved once.
- After `git fetch origin ${BASE_BRANCH}`, each crate gets one state: `done` (on crates.io, GitHub release present, and neither `HEAD` nor `origin/${BASE_BRANCH}` holds the release version), `push` (the STEP 11 commit is local only), `resume` (Phase 41 rule) or `new`. A `done` crate is reported and skipped.
- The plan block lists each crate, its state, version, next dev version and public pushes; one yes covers it and replaces the STEP 6 and STEP 11 confirmations. Dry-run lists and asks nothing. A nonzero patch part counts as a patch release for the next dev version; a resume reads the version from the parent of the version commit.
- After the yes, a `push` crate runs `git push origin ${BASE_BRANCH}` (dry-run prints `[DRY-RUN] Would push ${BASE_BRANCH}`); other crates run the single-package pipeline. On a failure the run checks out `${BASE_BRANCH}` when the tree is clean and prints `rerun: /release <crate>@<version> ...` with every crate. The final report lists each crate's version, tag and release URL.
- STEP 7 ends with `scripts/release/post_publish_hook.sh <crate> <version> <sha> [--dry-run]`; `<sha>` is `git merge-base ${BASE_BRANCH} HEAD` (the version commit in hotfix mode, `HEAD` in dry-run). It runs `<repo root>/.claude/config/post_publish.sh` with bash from the repo root, with `CRATE`, `VERSION` and `SHA` exported. It prints `post-publish hook: absent`, `done`, or `failed (exit N)` plus a `rerun:` line on stderr and exits 1; a failure stops before STEP 8. It refuses a version with a leading `v` and a SHA that is not a 40-hex commit.

**Files:**
- `commands/release.md` — Multi-Package Mode, `publish_order` in Configuration, the STEP 1 stop, the STEP 7 hook
- `scripts/release/post_publish_hook.sh` — the hook runner
- `scripts/release/test_release.py` — hook runner tests: absent hook, env names, nonzero exit, bad version, bad SHA

**Gotchas:** hana main has no `publish_order` or `github_release_repo` yet; hana-merges owns those keys and `post_publish.sh`.

### Phase 43 — `hana_shot locate` prints where to click a named target  · status: done
**Depends on:** none

#### As-built

- `hana_shot.py locate --port N --target <selector> [--json]` reads the current camera (it waits with `Session.settle` while the camera moves) and prints `<selector> <x> <y> <width> <height> <cx> <cy> scale <s>`, then `clipped` and `occluded` when set. Values are window logical pixels (physical / the camera's `scale_factor`), the space brp_extras `move_mouse`, `click_mouse` and `drag_mouse` take; the scale prints unrounded. `--json` prints `selector`, `entity`, `rect`, `click`, `scale_factor`, `window`, `clipped`, `occluded`. It never moves the camera, takes no shot and writes no timing line.
- One shared projection (`project_bounds`) serves locate and `--crop entity`; the crop output is unchanged. A corner behind the camera is dropped and a rectangle past a window edge is cut; both mark `clipped`. A camera with a viewport is refused.
- Click point: rays from the camera (`inverse_projection`, `window_ray`) through a `locate_grid` x `locate_grid` grid hit each visible `Aabb` in its `GlobalTransform` with a slab test (`box_hit_distance`); `ray_owner` gives each ray to the nearest hit. The click point is the target-owned point nearest the rectangle centre; `occluded` marks a centre owned by another entity.
- Rays follow bevy_picking: a mesh whose `Pickable` has `should_block_lower: false` never covers unless it is a target member, and a box that holds the ray origin is not hit unless it carries `RayCastBackfaces`.
- Exit 1 prints one stderr line: `off screen`, `behind the camera`, `covered by <name or entity:id>`, `no clear click point on <selector>` (no ray hits a box), or the no-visible-mesh-bounds failure with its parent hint. Exit 2 is a refused request.
- Selectors: `<outer>/<inner>` keeps inner matches under the one outer match; a missing outer prints `<scoped>: outer X found nothing`. A Show scope also reaches the entities whose `DrawnByShow` names it, plus their descendants; a Hana that does not reflect `DrawnByShow` reaches only the Show's children, and the failure text says so. `tool:<Kind>#-n` counts from the newest tool of that kind.

**Files:**
- `scripts/hana_shot/hana_shot.py` — `locate`, `project_bounds`, the ray and box hit test, scoped and `#-n` selectors
- `scripts/hana_shot/test_hana_shot.py` — locate tests on the `Fake` BRP
- `config/hana_shot.conf` — `locate_grid=9`
- `commands/hana_shot.md` — the selector list and the locate item

**Gotchas:**
- The hit test uses each mesh's box, not its triangles, so a point near a round edge can miss.
- `covered by` means a click hits the cover. A cable drag can still land on a covered jack, because Hana's drop rule picks the nearest jack under the hit's root; locate does not model that rule. The Show's rear jack reads `covered by Tool Visual Root` for this reason.
- The default scene holds its own Shapes and Shows, so a new tool is `#-1`, not `#1`.
- A beamed part is not a child of its Show; it carries `DrawnByShow` (`hana_mimesis_tools::show::DrawnByShow`), reflected from Hana 5593161498.

**Ruled out:** a triangle hit test; moving the camera to make a target clear.

### Phase 44 — The Disks measure walks each drive in parallel, both drives at once  · status: done
**Depends on:** none

#### As-built

- `scripts/disks/measure.py` walks each drive with dust, both drives at the same time (one `ThreadPoolExecutor` thread for each drive). `disks.json` keeps the config order of `drives`; `render_seconds` is the time of the whole measure. Live on natedev under build load: 52.0 s and 59.2 s (148 s with one du for each drive, one drive after the other); root totals match du within 0.1 %.
- `dust_command(mount, settings)`: `systemd-run --user --scope --quiet -p MemoryMax=<measure.memory_max> -- nice -n 19 dust -x -D -j -P -o b -z <keep_bytes - 1> -d <measure.max_depth> -n <measure.max_lines> --config <scripts/disks/dust.toml> <mount>`.
- `parse_dust(output)` reads dust's nested JSON (`size` such as `"2327020666880B"`, `name` a full path) into the `dict[str, int]` that `folder_tree` takes; the tree, `other` and the collapse rule are unchanged.
- Errors: a timeout; `the measure did not start: dust is not on PATH` when `shutil.which("dust")` finds nothing; `dust exited N: <last stderr line>` for a nonzero exit or no output. A folder dust cannot read leaves out its size, and the walk still counts as a measure.
- dust comes from nixos `modules/common/development.nix` (550a77b) on natedev and the Mac.

**Files:**
- `scripts/disks/measure.py` — `dust_command`, `parse_dust`, the concurrent `measure`
- `scripts/disks/dust.toml` — empty dust config; dust never reads a personal config
- `scripts/disks/test_measure.py` — the dust command, the JSON parse, both drives at once, the error forms
- `config/disks.conf` — `measure.max_depth` and `measure.max_lines` (1000000 each)

**Gotchas:**
- dust keeps a folder only when its size is above `-z`, so the measure passes `keep_bytes - 1` to keep a folder of exactly `keep_bytes`.
- dust reads a personal config unless `--config` names a `.toml` file; it rejects `/dev/null` as a config.
- dust gives partial JSON and exit 0 for unreadable folders; a missing path gives exit 1 and no output.
- natedev's NVMe drives use IO scheduler `none`, so `ionice` has no effect. The walk's inode and dentry cache is larger than the 2 GiB cap, so repeat walks are not faster.

**Ruled out:**
- A du fan-out over subfolders: each du process counts a hard link again, so the Nix store and cargo `deps/` count twice.
- A threaded Python walker: the GIL holds it near 130k entries/s (/ 58–68 s).

### Phase 45 — A refresh button on the Disks page measures the drives at once  · status: done
**Depends on:** Phase 44

#### Work Order

**Goal:** the user (change review C1825, 2026-11-17): "a refresh icon (lucide) that when pressed, would cause it to remeasure". The Disks page has a Lucide `refresh-cw` button. A press starts a measure at once, also when the sizes are current, and the status line shows Measuring. A press while a measure runs starts no second measure (showrunner, 2026-11-17).

**Spec:**
- `scripts/disks/disks_page.py` — `start_measure(now, launch=launch_measure, *, force=False)`: with `force`, skip the `cache_seconds` check. The lock rule stays: a running measure gives `MeasureStart.RUNNING` and no launch.
- `scripts/review_links/serve.py` — `disks_measure_post` reads the posted form. `force=1` passes `force=True`; an empty body keeps today's behavior. The answer keeps its form (`state`, `slots`, `sessions`).
- `scripts/disks/disks.html`:
  - A `<button type="button">` with the vendored Lucide `refresh-cw` icon (`scripts/review_links/lucide/refresh-cw.svg`, put in by `disks_page.publish` through `page_frame.icon_svg`; no hand-copied path), `aria-label="Measure now"` and a tooltip "Measure now". It sits at the end of the page status line, in the same row.
  - A press posts `force=1` to the measure name, sets the page's `measuring` state as `askMeasure` does, and runs a page refresh, so the status line shows `Paused · Measuring…` at once (the existing busy path).
  - While `measuring` is set, the button is disabled. It is enabled again when new data arrives or the wait ends (`settings.wait_seconds`).
  - A failed press shows its reason through the existing `failure` path; the button is enabled again.
  - Style: the icon is 16 px, `currentColor`, with the page's muted text color and the accent color on hover and focus; a visible focus ring. Use console theme tokens only; no new color.

**Files:**
- `scripts/disks/disks_page.py` — `start_measure` force
- `scripts/disks/disks.html` — the button and its press
- `scripts/review_links/serve.py` — `force=1` in `disks_measure_post`
- `scripts/disks/test_disks_page.py` — tests
- `scripts/review_links/test_serve.py` — tests

**Workers:** 1 writer — `impl` writes all five files.

**State:** none

**Constraints from prior phases:** a measure takes about 55–60 s on natedev (dust, both drives at once). `start_measure` never waits for the measure. `disks_page.publish` writes the page only when it changes. Tests write a fixture config; no test reads a tunable value. Every tunable goes in a config file.

**Acceptance gate:**
1. `scripts.disks.test_disks_page` passes, with tests for: `start_measure(force=True)` launches when the data is current; `force=True` with the lock held gives `RUNNING` and no launch; the published page holds the `refresh-cw` button with its label.
2. `scripts.review_links.test_serve` passes, with a test that `force=1` reaches `start_measure` and an empty body does not force.
3. basedpyright reports 0 errors and 0 warnings on the changed Python files.
4. Live on natedev (director): press the button on the Disks page in a browser. The status line shows `Paused · Measuring…` at once, the button is disabled, and a second press does nothing. About a minute later the sizes update and the button is enabled again. One dark desktop screenshot, clipped to the status line and button, for the Change-Review line with `| after restart`.

### Phase 46 — A question title is one short line, and the Review page never shows a long one  · status: done
**Depends on:** none

#### Work Order

**Goal:** the user (2026-11-17, via builds and the showrunner): "the item description is ridiculously long and unreadable - we need to have that be an impossible thing to happen". spacetime/Q461 and Q462 filed the whole question (context, values, choices, recommendation) as the item title, and the Review page shows 15+ lines in the Item column. The writer refuses a long title, and the Review page cuts an old long title to one short line.

**Spec:**
- `config/production.conf` — new key `question_title_max_chars=100` with a comment and a `# whole: 20..` line, as the other keys carry.
- `scripts/production/update_registration.py`:
  - `ask` refuses `--text` and `--ask` when either is longer than `question_title_max_chars` characters (the existing one-line check stays). The refusal names the limit and the length, and says to put the rest in `--context` and `--item`. It runs before the question review, so a refused question costs no review.
  - The `questions --state` path (the replacement table from a showrunner's `open_for_user`) refuses a new or changed item whose text is longer than the limit or longer than one line. This is the path that filed spacetime/Q461 and Q462. A stored item that is already longer is only named, as `plumbing` problems are (`report("questions", "reword", …)`).
  - Every other path that writes a question title: search for writers of `OutstandingItem` `text` and `parts["ask"]`, and apply the same rule through one helper, so the limit lives in one place.
  - The `--text` and `--ask` help text names the limit.
- `scripts/review_links/review_page.py`: the row title of every review item shows only its first line, cut at `question_title_max_chars` characters with "…" when longer. The search attribute keeps the full text. When the title is cut, the details show the full text (a question whose `ask` equals its `text` shows the full `ask` as its lead). Keep this change small: admin-console also changes this file now.
- `commands/showrunner/produce.md`, **Open-questions list**: today it says "Keep each item complete enough to stand alone when the user asks for the list: what is asked, where, why and the choices". That text made spacetime put the whole question in the item. Replace it: an `open_for_user` item is the short one-line title (at most `question_title_max_chars`); file a new question with `update_registration.py ask` and its parts (`--ask`, `--context`, `--item`, `--choice`, `--recommend`), so the page and `/next` show the rest.
- `docs/question_template.md` — the `--ask` line (line 12) names the title limit and says the rest goes in `--context` and `--item`. Both question commands import this file, so the rule lives in one place; `commands/showrunner/question.md`, `commands/unit/question.md` and the producer docs need no change.

**Files:**
- `config/production.conf` — the limit
- `scripts/production/update_registration.py` — the refusal
- `scripts/production/test_update_registration_questions.py` — tests
- `scripts/review_links/review_page.py` — the cut title
- `scripts/review_links/test_review_page.py` — tests
- `docs/question_template.md` — the limit line
- `commands/showrunner/produce.md` — the filing rule

**Workers:** 1 writer — `impl` writes all files.

**State:** none

**Constraints from prior phases:** tests write a fixture config; no test reads a tunable value. A changed config key needs the showrunner's go; this key is new. `review_page.py` is shared with admin-console: keep the hunk small and touch no other part of the file.

**Acceptance gate:**
1. `scripts.production.test_update_registration_questions` passes, with tests for: `ask` refuses a `--text` and an `--ask` one character over the limit and names the limit; a title at the limit files; the `questions --state` path refuses a new long or multi-line item and only names a stored one.
2. `scripts.review_links.test_review_page` passes, with a test that a long title shows one line cut with "…", the search attribute keeps the full text, and the details show the full text.
3. basedpyright reports 0 errors and 0 warnings on the changed Python files.
4. Live on natedev (director): the Review page shows spacetime/Q461 and Q462 (or a fixture copy when they are closed) as one line each. One dark desktop screenshot of those rows for the Change-Review line with `| after restart`.

### Phase 47 — A rename of a unit to its own name keeps bare worker names quiet  · status: done
**Depends on:** none

#### Work Order

**Goal:** the showrunner (2026-11-17, renamequiet). `agents rename OWNER OWNER` migrates old qualified worker names. For cargo-liner-style, whose worker rows already have bare names (`review`, `review2`, `style-ticket-1493`, `held-check-N`), it printed about 110 lines `kept; prefix does not end with cargo-liner-style`, each with a wrong hint such as `rename <id> 1493`. A row with no qualified prefix for this owner is already correct. The command prints no per-row line for it and never prints a hint that cuts a name. Second case (hana's widget): 14 rows carry the unit's former name as prefix (`widget-enhancements-impl`). Accept a prefix that equals or ends with a recorded former name of the owner; use only recorded former names, never a former name inferred from the shape of a worker name.

**Spec:**
- `scripts/agents/shutdown.py` `rename_qualified_workers(owner)`:
  1. The owner's qualified prefixes are its name and each recorded former name. A prefix qualifies when it equals one of them or ends with `-<one of them>`.
  2. A row whose name has no qualifying prefix is left as it is, with no per-row output. At the end, when one or more rows were left, print one line: `<N> worker names unchanged: no qualified prefix for <owner>`. No hint.
  3. Former names: find where a durable record of the owner's former names exists (the staff row, or the session record `formerNames` that `rename` writes, shutdown.py ~2181). Use a record only if it survives a session restart. If no durable record exists, accept only the owner's current name, and say so in the report.
  4. Qualified rows migrate as now.
- Live defect (showrunner, 2026-11-17): hana's organon and startup were renamed to `organon-unit` and `startup-unit`. `rename_member` resolves `<name>-unit` to the unit `<name>`, and the rename then treated `<name>-unit` as a new name. For startup it wrote a pending folder and branch move that no command can clear. The STOP on renames lifts only when this defect is gone.
  5. In `rename_plan` for a unit row, a new name equal to the row's name plus `-unit` is the row's own name: a same-name rename (worker migration and display repair only).
  6. A new rename replaces an unstarted pending rename: staff path and checkout at the record's `source`, branch at `source_branch`, `destination` absent, move lock free. The test lives in `scripts/production/unit_move.py`; the removal runs under the row lock in `rename_apply_unit`. A started move keeps today's refusal.
  7. On startup's state, `agents rename 556caf6e-713d-47c0-a8fc-5dd5e8c26ce4 startup` removes the record, renames the row back to `startup`, moves no folder or branch, and restarts no session.

**Files:**
- `scripts/agents/shutdown.py` — `rename_qualified_workers`
- `scripts/agents/test_shutdown_cli.py` — tests (the `UNDECIDED` scenarios at ~2234 pin the old kept line and hint; the director approves replacing them with the new behavior)
- `scripts/production/unit_move.py`, `scripts/production/test_unit_move.py` — the unstarted pending-rename test (items 6–7)

**Workers:** 1 writer.

**State:** none

**Constraints from prior phases:** test with fixture staff rows only; no run on live units. No new check or gate (showrunner).

**Acceptance gate:**
1. `scripts.agents.test_shutdown_cli` passes, with tests for: bare rows (`review`, `review2`, `style-ticket-1493`, `held-check-3`) print no per-row line and one summary line; a row with a recorded former-name prefix migrates; a row with an unrecorded foreign prefix stays and prints no hint.
   `scripts.production.test_unit_move` passes, with tests for items 5–7 and for a started move that still refuses.
2. basedpyright reports 0 errors and 0 warnings on the changed files.

### Phase 48 — An image can carry a question, and its answer comes back to the user  · status: done
**Depends on:** none

#### Work Order

**Goal:** the user (2026-11-17, via the showrunner, imageask): "images should have an ask question so that the image comes back with an answer - just like other things - if i say approved or not approved or send feedback those might not come back to me although they might - but ask a question must come back to me with an answer". unit-board's listask builds the same question flow into the List; the two pages share one module (agreed with unit-board 2026-11-17), with no second copy.

**Spec:**
- New `scripts/review_links/page_question.py`, the shared piece (form agreed with unit-board):
  1. Typed records `Question(question_id, asked_at, words, answer: Answer | None)` and `Answer(text, answered_at)`. `question_id` is `Q<n>`, numbered per item. Each page stores the questions on the item's own record.
  2. A `QuestionStore` protocol that each page implements on its own record under that record's lock: `questions(item)`, `add(item, Question)`, `set_answer(item, question_id, Answer)`.
  3. `ask(store, item, owner, source_path, row, title, words, answer_command, status) -> MessageDelivery`. It takes the next id, saves the question under one delivery claim, and sends `<Page> <item>: Ask question` / title / words / `Answer with this command: <command with item and Q id>`. It reuses the Review page delivery (`change_page.DeliveryStatus` claim, `MessageDelivery`, retry), as `review_actions.send` does.
  4. `answer(store, item, question_id, text) -> Answered | AnswerRefused`. It refuses an unknown or already-answered id, then writes the answer. Each page runs its own return step after it.
  5. `open_question_html(question)` and `answered_question_html(question)` render the open line and the question-and-answer pair.
- `scripts/review_links/images.py` and `image_store.py`:
  1. Each card gets an **Ask question** button beside Send feedback. It uses the feedback text field; an empty field gives "Enter a question first."
  2. The question goes to the image owner with the image id and the question id. The image is not deleted. The card stays on the page and shows the open question. While a question is open, the image does not count as waiting for the user (console new mark).
  3. `images.py answer --id <image> --question Q<n> --text "<answer>"` calls `page_question.answer`. The card then shows the question and the answer, and the image counts as waiting for the user again (the return step: it clears `feedback_at` if set and marks the card new).
  4. The image record type gains the questions field as `NotRequired`, so older records still read.
- `commands/show_image.md` (or the command that tells owners about Images answers): one line naming the answer command.

**Files:** `scripts/review_links/page_question.py` and `test_page_question.py` (new); `scripts/review_links/images.py`, `image_store.py`, `test_images.py`; the show_image command doc.

**Workers:** 1 writer (`design`); the `test` worker takes the live shot.

**State:** image records gain an optional questions field; no migration.

**Constraints from prior phases:** tests write a fixture config; no test reads a tunable value. No mockup (showrunner). `page_question.py` is shared with unit-board's listask: keep its public surface to the form above; send unit-board its path as soon as its tests pass.

**Acceptance gate:**
1. `scripts.review_links.test_page_question` and `scripts.review_links.test_images` pass, with tests for: next id per item; ask saves and sends once under the claim; a failed send keeps the question with its delivery state; answer refuses an unknown and an answered id; the image card shows the open line, then the answered pair; the image leaves and re-enters the waiting set; an older record without questions reads.
2. basedpyright reports 0 errors and 0 warnings on the changed Python files.
3. Live on natedev (director, by the `test` worker on a copy): ask a question on a fixture image, answer it with the command, and the card shows the question and the answer. One dark desktop screenshot for the Change-Review line with `| after restart`.

### Phase 49 — The fast-track recovery reads each Git object once, in one batch  · status: todo
**Depends on:** none

#### Work Order

**Goal:** the showrunner (2026-11-17, fasttrackcost): "production/test_fast_track.py costs 55.6 aws CPU s ... most of the cost is about 6700 git calls from GitRunner recovery reads in fast_track.py. Batch the immutable object reads; keep the mutable ref reads and all writes as they are. ... Measure aws CPU before and after, and put both in the checkpoint."

**Spec:**
1. In `scripts/production/fast_track.py`, find which recovery reads make the Git calls in `test_fast_track.py`. Count them by command with `module_cost.py --commands`.
2. Batch the reads of immutable objects (an object addressed by its full hash: `cat-file -e <hash>^{commit}`, blob and tree reads by hash). Use one `git cat-file --batch-check` or `--batch` process for each recovery pass, through `GitRunner`. Keep the `GitRunner.reconcile_reads` cache rules: no cached failed read, and no cache outside one pass.
3. Keep each read of a mutable ref (`rev-parse HEAD`, `rev-parse origin/main`, `merge-base --is-ancestor`, ref lists) as one fresh call. Keep every write as it is.
4. The behavior of `reconcile`, `push_patch` and `restore_prepared` does not change. No existing assertion changes.

**Files:**
- `scripts/production/fast_track.py`
- `scripts/production/test_fast_track.py` — only a new test of the batch

**Workers:** 1 writer (`repo-fix4`).

**State:** none.

**Constraints from prior phases:** tests write a fixture config; no test reads a tunable value.

**Acceptance gate:**
1. `scripts.production.test_fast_track` passes, with a test that one recovery pass starts one batch process for its object reads.
2. basedpyright reports 0 errors and 0 warnings on the changed Python files.
3. `python3 ~/.claude/scripts/test_times/module_cost.py --tree <worktree> production/test_fast_track.py --commands 10` before and after the change: CPU seconds and Git process starts. Both go in the checkpoint.

### Phase 50 — A new change row fills an empty cell for each optional column  · status: done
**Depends on:** none

#### Work Order

**Goal:** the showrunner (2026-11-17, reviewlogcol, urgent): since about 10:25 EST no change row can be added to the natedev change list, for every production. Every merge prints "change review: failed — change-review - natedev.md: no value for the `Review log` column". A review event adds the `Review log` column to the To review table (`_add_column`), and the new-row code builds its values with no `Review log` key, so `_row` refuses the row.

**Spec:**
1. In `scripts/production/change_review.py`, a new To review row gets an empty cell for each table column it has no value for, as the Reviewed insert already does (`dict.fromkeys`). One rule for every optional column, not a special case for `Review log`.
2. Keep `_row` as it is: it refuses a table column with no value and ignores a value with no column. Keep every value the new-row code writes today.
3. Branch from origin/main 9d7badf17. No edit of the real change list.

**Files:**
- `scripts/production/change_review.py`
- `scripts/production/test_change_review.py` — a test with a fixture list whose To review table has a `Review log` column

**Workers:** 1 writer (`design`).

**State:** none.

**Constraints from prior phases:** tests write a fixture config; no test reads a tunable value.

**Acceptance gate:**
1. `scripts.production.test_change_review` passes, with a test that `add`, `list-ready` and `live` add a row to a fixture list whose To review table has a `Review log` column; the test fails on origin/main.
2. basedpyright reports 0 errors and 0 warnings on the changed Python files.
3. `git merge-tree` of the checkpoint against devtools reviewscan d0db535e2 and admin-console waitbuttons-2 cca12f3a3 reports the result.

### Phase 51 — An image question never reads as unsent when it was sent  · status: todo
**Depends on:** Phase 48

#### Work Order

**Goal:** the showrunner (2026-11-17) took both phase 48 add-ons as next work. Two edges of the image question delivery show a false `Not sent`.

**Spec:**
1. A page opened during server startup, before startup recovery claims a saved delivery intent, shows the question as sending, not `Not sent`, and keeps polling until recovery ends.
2. A send that succeeds and then fails to write its result is not shown as `Not sent`, and a retry never sends the same question twice. Record the send before the result write, or check that record before a retry; pick the simplest rule that is always correct.

**Files:**
- `scripts/review_links/change_page.py`
- `scripts/review_links/images.py`
- `scripts/review_links/serve.py`
- `scripts/review_links/test_images.py`
- `scripts/review_links/test_change_page.py`
- `scripts/review_links/test_serve.py`

**Workers:** 1 writer (`design`).

**State:** none.

**Constraints from prior phases:** tests write a fixture config; no test reads a tunable value. Keep phase 48 behavior (F001–F011) unchanged.

**Acceptance gate:**
1. The review_links suites pass, with a test for each edge that fails on phase 48.
2. basedpyright reports 0 errors and 0 warnings on the changed Python files.

### Phase 52 — A rename to a name the folder and branch already carry prints no anomaly  · status: done
**Depends on:** none

#### Work Order

**Goal:** the showrunner (2026-11-17). hana ran `agents rename 556caf6e-713d-47c0-a8fc-5dd5e8c26ce4 startup` on the row named `startup-unit`. The rename was right: the row name is `startup`, and the folder `hana_catalyst-startup` and branch `tool-based-ui-startup` stay. But it printed two false anomaly lines: "folder … does not end with -startup-unit" and "branch … does not end with -startup-unit". The folder and branch already end with the new name, so nothing needs a move.

**Spec:**
1. In `rename_apply_unit` (`scripts/agents/shutdown.py` ~2276), a folder or branch that already ends with `-<new name>` needs no move and prints no anomaly.
2. A folder or branch that ends with neither the old nor the new name still prints its anomaly line.
3. Use the name rule from `unit_names` that phase 47 added (a name and that name plus `-unit` are the same name); do not write a second copy.

**Files:**
- `scripts/agents/shutdown.py`
- `scripts/agents/test_shutdown_cli.py`

**Workers:** 1 writer.

**State:** none.

**Constraints from prior phases:** tests write a fixture config and fixture staff rows; no test runs a live rename.

**Acceptance gate:**
1. `scripts.agents.test_shutdown_cli` passes, with a test for the `startup-unit` to `startup` case (no anomaly lines, folder and branch unchanged) and one for a folder that ends with neither name (anomaly kept).
2. basedpyright reports 0 errors and 0 warnings on the changed Python files.

### Phase 6 — Each phase records its waits and their cause, and each merge leaves a receipt  · status: done
**Depends on:** Phase 3

#### As-built

- `progress_history.py start-wait --kind <kind> [--reason <text>]` prints a wait id, and `finish-wait --wait-id <id>` closes that one wait. Events (`wait_started`, `wait_finished`, `unclosed`) carry the phase instance id, the display phase id, unit, owner session and UTC times. Kinds: `WaitingForQuotaRefusal` (a provider refusal only, never a quota reading), `WaitingForMemoryOrSlot`, `WaitingForMergeGate`, `WaitingForUser`, `WaitingForSupplier`, `ReadyWithoutCapacity`. A probe session writes no wait. An open wait at `finish-phase` closes as `unclosed` with its start and no end.
- Emit points: `verify.sh` (memory admission, cargo token, build hold); `merge_checkpoint.py` (merge gate); the Stop hook starts a user wait on a `decision:`/`gate:` turn and a supplier wait on a `blocked:` turn, and the UserPromptSubmit hook closes it through `turn_wait.json`.
- The `waits` verb returns waits for `[since, until)` with `covered_n` and `missing_n`. `start-phase` marks the instance as covered; a covered phase with no wait reads as covered zero, and a phase from a run with no wait history reads `HistoryNotRecorded`, never 0.
- Each pushed merge on both paths (checkpoint and child handoff) appends one receipt to `merge_receipts.jsonl` under the history root: project (from `Repository`), issue or plan phase, merge commit, source checkpoint, target branch, UTC time. A retry writes no second receipt; a rolled-back merge writes none. The `receipts` verb names a merge commit with no receipt `ReceiptMissing`.
- `phase_process.py baseline`'s trial section reports median and p90 phase time per unit, with n, over the same eligible trial phases as its three measures. `history.py` and `rounds.py` still read old run files.
- Wait attribution is incomplete: the slot comes from the shared app-server's start environment, several matching phase instances read as covered zero, and any user prompt closes a user or supplier wait. The phase "Each wait names the worker, checkpoint and sender that own it" fixes these.

**Files:**
- `scripts/direct/progress_history.py` — the recorder, the `waits` and `receipts` verbs; every emit point calls it
- `scripts/direct/phase_process.py` — trial phase time
- `scripts/direct/verify.sh`, `scripts/production/merge_checkpoint.py`, `scripts/hooks/stop-direct-continue.py`, `scripts/hooks/user-prompt-submit-conversation-pause.py` — emit points and receipts
- `scripts/hooks/test_direct_turn_wait_hooks.py`, `scripts/production/test_merge_checkpoint.py` — hook wait and receipt tests

**Binds later work:** the wait event shape, the six kinds, `HistoryNotRecorded` versus covered zero, and the receipt fields go to hierarchies-unit as a schema handoff; no script in this tree reads wait events yet. The baseline freeze records trial phase time from `phase_process.py baseline`.

**Gotchas:**
- A unit finishes its phase before the showrunner merges it, so a merge-time record resolves a finished phase instance.
- A new import in a shared script breaks the fixture copy lists in `test_review_launcher.py` and `test_update_registration.py`.
- `phase_reservation.py` committed does not run in a repository that is not enrolled.

**Ruled out:** the slot taken from the shared app-server's start environment as a pass selector (it is fixed for every thread); choosing the newest phase instance for an ambiguous merge (it can name the wrong instance).

### Phase 7 — Each wait names the worker, checkpoint and sender that own it  · status: done
**Depends on:** Phase 6

#### As-built

- Historical worker waits (2026-10-29): `verify.sh` supplied `CODEX_THREAD_ID`. The mesh controller supplied the worker name, run directory and thread id. `resolve_worker_pass` mapped the thread through `mesh_roster.json` to one active pass. Commit `ce529466f` removed the mesh controller on 2026-11-08. The deployment record in Phase 4 identifies the earlier revisions.
- Merge-gate waits: `<CheckpointCommit/>` runs `progress_history.py checkpoint --session-dir <S> --phase <id> --commit <hash>` after each commit, in every repository. `merge_checkpoint.py` turns the notice's hash into the full id (`git rev-parse --verify <commit>^{commit}`) and finds that event across the run files under the history root, this worktree's phases included. One match attributes the wait. Zero or several matches read as uncertain coverage for each candidate, never covered zero. A child handoff records a production-level handoff wait with no phase.
- User and supplier waits: the UserPromptSubmit hook ends `WaitingForUser` on typed input, or on a `From the user (via the showrunner):` relay from the expected showrunner address. It ends `WaitingForSupplier` only on a message from the supplier's current address. The production comes from the plan header through `resolve_production_unit`, which reads metadata only and accepts both header forms. Timers, task notices and other peers leave both open. The duplicate check compares the supplier too. Waits a phase leaves open close as `Unclosed` at phase completion, external waits included, under a shared lock.
- `waits` JSON reports `covered_n`, `attribution_miss_n`, `uncertain_phase_n`, `missing_n`, `production_handoff_n` and the handoffs, each miss with its reason and candidates.
- `memory_gate.sh` holds the memory-line body in one inner function; `buildlog_memory_line` and `verify.sh` call it.
- `phase_process.py baseline` prints durations of 2 h or more in hours.
- `workers.py` reads the production through `resolve_production_unit` and keeps `showrunners.canonical_slug` for the ledger path.

**Files:**
- `scripts/direct/progress_history.py` — resolver, attribution types, checkpoint verb, supplier identity, uncertain coverage, `waits` JSON
- `scripts/hooks/user-prompt-submit-conversation-pause.py` — sender-matched completion and the showrunner relay
- `scripts/direct/verify.sh`, `scripts/lint/memory_gate.sh` — thread attribution and the shared memory line
- `scripts/production/merge_checkpoint.py` — full-hash checkpoint lookup and the child handoff wait
- `commands/unit/checkpoint.md` — the checkpoint verb line
- `scripts/direct/workers.py`, `scripts/direct/phase_process.py`

**Binds later work:** the `waits` JSON schema went to hierarchies-unit at 820357a2. A reply that the postmaster forwards does not end a supplier wait yet; Phase 8 fixes it.

**Gotchas:**
- A launcher that never exits keeps its subscription to the worker thread and records the next compaction turn as work, so the worker reads busy and refuses follow-ups. End only the stale launcher pids.
- A merge in a `git clone --shared` scratch copy must fetch from the GitHub URL: the copy's `origin` is the worktree, so its `origin/sdlc` is the local ref.

**Ruled out:** the slot taken from a variable fixed at app-server start; the newest phase instance for an ambiguous checkpoint; checking a postmaster-forwarded reply against its log before the log exists (removed as containment).

### Phase 8 — A supplier reply that the postmaster forwards ends its wait  · status: done
**Depends on:** Phase 7; postmaster-unit's reply record (main abc8344f)

#### As-built

- `progress_history.py` `_supplier_message(identity, prompt, owner_session)` returns `VerifiedSupplierMessage | UnverifiedSupplierMessage`, and `finish_turn_wait` passes `owner_session` to it. A verified message names its route: `SupplierMessageRoute.DIRECT_REPLY` or `SupplierMessageRoute.POSTMASTER_REPLY`.
- A postmaster forward ends a `WaitingForSupplier` wait only when every check passes, in this order: the outer sender resolves to the postmaster's own address (checked before any record is read); the forward text has the postmaster's `[<at_ms>-<n>]` form; exactly one valid record in `$XDG_STATE_HOME/message/postmaster/replies/<at_ms>-*.json` has that `at_ms`, a `socket` equal to the supplier's current address, a `body` equal to the forwarded body, and this session in `targets`.
- Each record is parsed once into a typed `PostmasterReplyRecord` (frozen dataclass; `from_fields` raises `ValueError` on a bad field). A symlink, a bad file name or an unreadable record is skipped. The reader's current form is in the phase "The postmaster reply reader reads write-once records".
- A failed check gives `UnverifiedSupplierMessage` with a `SupplierMessageRejection` (such as `OUTER_SENDER_NOT_POSTMASTER`, `NO_RECORD`, `SEVERAL_RECORDS`, `SOCKET_MISMATCH`, `BODY_MISMATCH`, `NOT_A_TARGET`), and the wait stays open. A direct supplier reply ends the wait as before.
- A verified reply alone never ends `WaitingForUser`; that still needs the expected showrunner's relay.

**Files:**
- `scripts/direct/progress_history.py` — forwarded-reply verification and the rejection enum
- `scripts/direct/test_progress_history.py` — pending-forward, rejection and direct-delivery tests
- `scripts/hooks/test_direct_turn_wait_hooks.py` — hook-level wait tests

**Gotchas:** the record file counter (`<n>` in the file name) is not the forward's suffix counter; the match uses `at_ms` only. `targets` proves the intended receiver while the forward is still pending; the confirmed ids are never part of the match.

**Ruled out:** checking a forwarded reply against a log that the postmaster writes after it forwards — the recipient's hook runs before that log exists.

### Phase 9 — A decision that makes a run useless cancels that run in the same turn  · status: done
**Depends on:** none

#### As-built

- `<WorkerRuns/>` in `commands/unit/direct.md` holds the run-cancel rule: when a decision makes a running or queued test, lint or build run useless (the phase is parked, the round failed, a newer tree replaces the run's tree, or a result in hand answers the question), cancel it in the same turn to free its memory and its place in the queue. No run finishes only to complete a report.
- Cancel steps by run kind. A detached run: run `detached_runs.py stop --reason "<why>" <record>` on its record in `${SESSION_DIR}/detached_runs/` (the `.json` beside its log). The verb checks the supervisor and the command leader, then sends TERM to the command group only; the supervisor delivers the stopped result. A detached `verify.sh` run: the same verb, because `verify.sh` detaches through `detached_runs.py` and handles TERM. A run in the memory queue: cancel its owning run; the queue drops dead callers on its next read. The queue has no cancel command of its own (`memory_admit.py release` only releases a reservation); the rule says so and points to `docs/plans/sdlc-run-cancel-design.md`.
- The park path and the repair-round path in `direct.md` point to `<WorkerRuns/>`. In `/showrunner:produce`, a ruling that parks, stops or replaces work names the runs to cancel.
- `docs/direct/write_prompt_contract.md` item 3 (`## Builds and tests`) holds the worker form: cancel a run of your own that a later edit or result makes useless, with `detached_runs.py stop --reason "<why>" <record>`.

**Files:**
- `commands/unit/direct.md` — the `<WorkerRuns/>` rule and its park and repair pointers
- `commands/showrunner/produce.md` — the ruling rule
- `docs/direct/write_prompt_contract.md` — the worker sentence in item 3

**Binds later work:** the short route keeps the complete `<WorkerRuns>` body, with this rule, in the always-read part of `direct.md`, and the worker sentence stays in `write_prompt_contract.md` item 3. Both texts and their park and repair references stay byte-identical across the move.

### Phase 10 — Findings and a design: checkable cancel criteria and one stop command  · status: done
**Depends on:** none

#### As-built

- `docs/plans/sdlc-run-cancel-design.md` holds findings and a design. No code changed.
- Findings: for each of the five cancel criteria (phase or reservation released, parked or failed; tree no longer the tip of a live branch or phase; unit recorded a stop or park; owning session gone; a newer run queued for the same phase and step), the doc names the source a tool reads, its reliability today and the check that closes the gap, with live natedev observations. No criterion is reliable today as an automatic trigger.
- Design: one proposed command, `scripts/direct/run_stop.py`, with command, round and phase forms. It writes one atomic stop record per run and a findings-ledger projection for a stopped round; exit 77 always means cancelled; one cancel id covers one command; the cleanup order releases a memory charge only when the owned execution is confirmed empty. The doc names each run kind's run-to-phase link today, the link it must gain, and the callers (director, showrunner, repair round). `run_stop.py` is a proposal, not a plan phase; it does not exist.
- The `verify.sh`, board and memory-queue part is marked as builds' part, for builds to agree.

**Files:**
- `docs/plans/sdlc-run-cancel-design.md` — findings, design, builds' part, open questions

### Phase 10b — The run-stop design carries its owners' answers  · status: done
**Depends on:** Phase 10

#### As-built

- `docs/plans/sdlc-run-cancel-design.md` holds each owner's answer to builds' part. The design defines no copy path. It points to the user's rule of 2026-10-29 (`/tmp/claude/copies/<showrunner>-<unit>/<phase>-<purpose>`, the folder name from `roster.UnitLabels.folder_name()` on labels from `roster.unit_labels_for`, owned by verify-unit's `docs/as-built/gate-checkouts.md`), and the run-stop provenance goes in a file inside that folder. `board.sh` is natedev's file.
- verify-unit agreed items 1, 3, 4 and 6: the launchers export the owner link before `verify.sh` refuses calls without it; one command id passes down in the environment; the TERM order builds on verify-unit's token fix (no reclaim from a live holder, token renewed with `--pid`); the Mac claim stays until the remote side is empty; one 15 s attempt deadline; only verify's own cancel path sets exit 77.
- resources-unit agreed item 2: charges bind to the step's systemd scope cgroup; a dead wrapper's kept charge counts measured memory; a step with no scope keeps today's rule; hold waits cancel through `interrupt-local`; a stopped command leaves a tombstone.
- Items still open stay under the doc's Open questions. No code changed.

**Files:**
- `docs/plans/sdlc-run-cancel-design.md` — the owners' answers

**Ruled out:** a second copy path rule in the design — the user's copies rule is the only one.

### Phase 11 — Each quota, memory and slot wait names who holds what it waits on  · status: done
**Depends on:** Phase 8

#### As-built

- `progress_history.py` holds a tagged holder: `NamedHolder(resource_kind, resource, holder)`, with `resource_kind` one of `account`, `slot`, `memory` and `holder` either `SessionHolder(session_id)` or `UnitHolder(unit)`; or `HolderNotNamed(reason)`. Each is a frozen dataclass that checks its fields; no empty string or null carries state.
- `start-wait` takes `--holder-resource-kind`, `--holder-resource` and one of `--holder-session` or `--holder-unit`, all or none. None records `HolderNotNamed("producer named no holder")`. A partial set, or holder flags on a kind other than `WaitingForQuotaRefusal`, `ReadyWithoutCapacity` and `WaitingForMemoryOrSlot`, exits 2.
- `wait-holder --session-dir D --wait-id ID <holder flags>` appends a `wait_holder_seen` event for an open wait, and exits 2 for a closed or unknown wait id.
- `waits` gives each wait `holders`: every holder seen, in order, each with its time. A record with no holder events reads `HolderNotNamed("recorded before holders")`.
- A wait event row with no `wait_id` counts in `ignored_n` and in the top-level `wait_without_id_n`. Its phase (same `run_id` and `phase_instance_id`) reads `HistoryNotRecorded` with reason `wait record has no wait id`.
- Each phase row carries `plan_doc`, `production` and `repository` as `{"state": "Recorded", "value": ...}` or `{"state": "NotRecorded", "reason": ...}` (`RecordedPhaseMetadata | PhaseMetadataNotRecorded`). `phase_started` records `production`, `unit` and `repository`. An older event resolves them once per run from disk: the plan header's `> **Production:**` line, with or without a unit part, and the worktree's git common directory.

**Files:**
- `scripts/direct/progress_history.py` — holder types and flags, `wait-holder`, the `holders` output, the wait-without-id count, phase source metadata
- `scripts/direct/test_progress_history.py` — CLI tests for each case

**Binds later work:** `verify.sh` passes the resource holder to the wait recorder and calls `wait-holder` when the holder changes. The holder identifies the slot owner, memory reservation owner or build hold owner. hierarchies-unit reads the holder field shape. The deleted mesh producer requires no further change.

**Gotchas:** most plan headers name the production with no unit part, so the production reader accepts both header forms.

### Phase 12 — The postmaster reply reader reads write-once records  · status: done
**Depends on:** Phase 8

#### As-built

- `PostmasterReplyRecord` holds `at_ms`, `socket`, `name`, `session_id`, `senders`, `body` and `targets`; it has no `forward` or `forwarded` field. A record written before the write-once change still carries those fields; the reader does not read them, and the record reads the same.
- `_read_postmaster_reply_record` reads a record once, with no re-read and no sleep. Unreadable or invalid JSON is a rejected record.
- The reader does not read `forwards/`: `targets` decides intent, and no check uses the confirmed ids. The match rules, rejections and wait ending of the phase "A supplier reply that the postmaster forwards ends its wait" are unchanged.

**Files:**
- `scripts/direct/progress_history.py` — the reply record type and its single read
- `scripts/direct/test_progress_history.py` — write-once, old-form and rejection tests

**Gotchas:** the postmaster writes `replies/<at_ms>-<n>.json` once before the first forward, and `forwards/<at_ms>-<n>.json` (`{forwarded}`) once after the last. No file is rewritten, so a reply record read after the forward is complete.

**Ruled out:** re-reading a torn record — a write-once record cannot be torn.

### Phase 13 — The trial measures table fits a phone  · status: done
**Depends on:** Phase 11

#### As-built

- The Trial measures table in `phase_process.py baseline` keeps every line at most 60 characters. Counts print in short form (`55K`, `2.4M`, `12.3B`, `1.0T`; values below 1,000 as they are), and a value that rounds up to 1,000 of one step prints in the next (999,999 is `1.0M`).
- Row labels are short (`weighted tokens`, `raw input with caches`, `raw output`, `compactions`). A second heading line says director tokens are phase plus gap and compactions are per phase. The title does not repeat the window.
- The table has no not-recorded column: each measure with a not-recorded count above 0 prints one line under the table, such as `not recorded: weighted tokens n=1`.
- The other tables and the `--detail trial` rows keep full values. The shared `table()`, `distribution_row()` and `count()` are unchanged.

**Files:**
- `scripts/direct/phase_process.py` — the short-form trial table
- `scripts/direct/test_phase_process.py` — width, short-form and not-recorded line tests

**Binds later work:** the trial table cells are short counts (`2.7M`) that hide differences near 1.5%. A Step log records each eligible phase's exact weighted tokens from the `--detail trial` rows (`str(row.tokens.weighted)` in `phase_process.py`), never the table cells.

### Phase 15 — A rerun review lens checks every change that writes lasting state  · status: done
**Depends on:** none

#### As-built

- `docs/direct/dual_review.md` `<TeamReview/>` has a third lens, `rerun`, with five questions, each with one real case: (1) after a stop at any line, does a rerun finish; (2) does it trust a stored status without checking the live thing (pid and process start time, path, lock holder); (3) does a failure move or delete what a rerun needs; (4) does one failure stop other, unrelated steps (`set -e` over a list); (5) does each mark have an expiry or an owner check, and a command that clears it. A failed answer is a defect, fixed in the phase. The prompt closes with the rerun case the reviewer built, or a statement that it could not build one.
- Arming: `<DualReview/>` runs `rerun_lens.py arm --diff <diff>` on the completed phase diff, made with whole-file context (`git diff --unified=100000`), before the broad review. `rerun` is armed when the Work Order's `**State:**` line names lasting state or the check prints `armed`. `none` or an absent line never stops a detected write from arming it, and a check error is not `not armed`. When armed, `rerun` launches with the other lenses on the same review copy, and its result is `review_findings_<pass>_rerun.txt`.
- `scripts/direct/rerun_lens.py arm --diff <file>` prints `armed` with each hit as `path:line — <kind>`, or `not armed`; it exits 2 on a read or parse error. It reads only. Kinds: file write or append, file move or delete, lock or claim, JSON state write, and fail-fast step lists. It reads Python, shell (`mv`, `rm`, `> file`, `mkdir` as a lock, `flock`, `set -e` over a list) and Rust (`fs::write`, `File::create`, `OpenOptions` with write or append, `fs::rename`, `remove_file`, `remove_dir_all`, `fs2`, `fd-lock`). Test files, fixtures, docs and `#[cfg(test)]` items do not count.
- The lens list exists once: `scripts/usage/agents_log.py` `Lens` (a `Literal` that includes `rerun`) and `LENSES = get_args(Lens)`. `assign.py --lens`, `findings.py` (`REVIEW_LENSES`, `open --lens`) and `review.sh --lens` read it. In `review.sh`, `adversary` takes the `test` slot, `contract` takes `impl`, and every other lens, `rerun` included, takes `review`.
- `docs/direct_plan_format.md` and `docs/direct/compose_work_order.md` define the optional `**State:**` field after `**Workers:**`; `berth.work_order validate` accepts a Work Order with or without it. The `findings.py open` row in `commands/unit/direct.md` lists `rerun`.

**Files:**
- `docs/direct/dual_review.md` — the lens, its questions and cases, the arming step
- `docs/direct/compose_work_order.md`, `docs/direct_plan_format.md` — the `**State:**` field
- `commands/unit/direct.md` — the lens list in the `findings.py open` row
- `scripts/direct/rerun_lens.py`, `scripts/direct/test_rerun_lens.py` — the diff check and its fixture tests
- `scripts/usage/agents_log.py` — the one lens list
- `scripts/direct/review.sh`, `scripts/direct/assign.py`, `scripts/direct/findings.py` — read the lens list
- `scripts/direct/test_review_launcher.py`, `test_review_launcher_pool.py`, `test_review_launcher_delivery.py`, `test_assign_launcher.py`, `test_findings.py`, `test_review_pool_report.py`, `test_progress_history.py`, `scripts/history/test_rounds.py`, `scripts/usage/test_agents_log.py` — accept `rerun`

**Binds later work:** a new lens is one edit to `Lens` in `scripts/usage/agents_log.py`.

### Phase 16 — One rerun sweep of the scripts names each fault and its owner  · status: done
**Depends on:** Phase 15

#### As-built

- `docs/plans/sdlc-rerun-sweep.md` lists the places in `~/.claude/scripts` where a stop or a failure leaves state that a rerun does not repair. It was read at sdlc `097b1a0d` with the five `rerun` lens questions, over every script that `rerun_lens.py arm` marks armed on the whole file. Each row has `path:line`, the question number, the rerun result and the owner unit (from the file's last checkpoint subject; `unknown` when no checkpoint names the file). Rows are grouped by owner. The unit director checked each row against the tip. The sweep fixes nothing; the showrunner routes each row.
- `progress_history.py waits` keeps malformed holder evidence visible: a present `holder` that is not an object, or that has a field outside its state's schema, becomes `MalformedHolder` (`{"state": "MalformedHolder", "reason": ..., "raw_value": ...}`). Only an absent `holder` key reads `HolderNotNamed("recorded before holders")`. Terminal wait rows (`wait_finished`, `wait_unclosed`) that carry a holder are read the same way; a terminal row that repeats a holder already seen adds no observation.
- A phase's `production` in `waits` is a production name, never a path. A plan header or a stored `phase_started` value that names a production doc (a value with `/` or a `.md` suffix) is read through that doc's `# Production — <name>` heading. A doc that cannot be read or has no heading gives `PhaseMetadataNotRecorded` with the reason.
- `scripts/production/production_doc.py` (standard library only) has `production_name(lines) -> ProductionName | ProductionNameNotRecorded`. `production_lifecycle.production_name` reads through it and keeps its `LifecycleStop`. `progress_history.py` imports `production_doc` at module level, and no import cycle exists.

**Files:**
- `docs/plans/sdlc-rerun-sweep.md` — the sweep list, with its tip hash and time
- `scripts/direct/progress_history.py`, `scripts/direct/test_progress_history.py` — `MalformedHolder` and production names
- `scripts/production/production_doc.py`, `scripts/production/production_lifecycle.py`, `scripts/production/test_production_lifecycle.py` — the shared production name reader
- `scripts/direct/review_launcher_test_support.py`, `scripts/direct/implement_launcher_test_support.py` — their copied scripts tree holds `production_doc.py`

**Binds later work:** the rerun-fault rows routed to their owners come from `docs/plans/sdlc-rerun-sweep.md`, checked at `097b1a0d`; line numbers there are from that tip. A reader of `holders` that counts holder evidence treats `MalformedHolder` as not valid evidence and shows it.

**Gotchas:** a new import in `progress_history.py` must also go into the copied scripts trees of `review_launcher_test_support.py` and `implement_launcher_test_support.py`.

### Phase 14 — A plan's Depends line counts only this plan's own phases  · status: done
**Depends on:** Phase 15 (order only: the showrunner ranks the rerun lens first)

#### As-built

- `scripts/direct/plan_history.py` reads a phase's `**Depends on:**` ids only from `Phase <id>` and `Phases <id>, <id> and <id>` spans. The keywords match in any case; the id suffix is lowercase. An id followed by a word character, or by `-`, `:`, `.` or `/` and a word character, is not an id, so dates, times, hashes and durations (`2-week`) never count. Numbers outside a span (`Steps 1 and 4`) never count. `none` gives an empty list; an absent line stays `None`.
- A span that follows another unit's name (`models-unit's Phase 4`, also in backticks, quotes or bold) is that unit's phase and is skipped. A span that follows the plan's own unit name, read from its ``> **Production:** … — unit `<unit>` `` header, counts.
- `phase_table.py` reads `depends_on` unchanged, and a named id that is not a phase of the plan still prints `not in the plan`.

**Files:**
- `scripts/direct/plan_history.py` — the dependency parser (`PHASE_REFERENCE_PATTERN`, `UNIT_PHASE_PREFIX`, `PLAN_UNIT_HEADER`)
- `scripts/direct/test_plan_history.py`, `scripts/direct/test_progress_history.py` — dependency-parse tests

### Phase 17 — Five rerun faults in the unit scripts clear on a rerun  · status: done

#### As-built

- A stop at each of five named steps no longer blocks a rerun. Each fault has a stop-and-rerun test that fails on the tree before its fix.
- `assign.py`: a launcher claim records the launcher's process start time beside its pid. A claim is live only when that pid exists with the same start time. A reused pid reads as stale, and the rerun takes the worker. A claim with no start time keeps the pid-only check.
- `findings.py` `landed`: a rerun after a stop between the state write and the `finding_batch_landed` event writes the missing event once and says so. A round whose event exists stays "no repair round in flight".
- `verify.sh`: a failed forced lint deletes the old pass record before it publishes its failure record, so an older pass never hides a failure on the same tree. When a resource wait close fails, cleanup keeps the wait id (or the folder), and the next run closes that wait before it opens its own. The wait file is published by a staged write.
- `end_session.sh`: each run is unbound and cleaned on its own. A failed `roles.py unbind-run` does not stop the other runs. The script exits nonzero at the end and names each failed run, so a rerun retries only those.
- `scripts/lib/process_start.py` is the one process start and /proc stat reader under `scripts/`. `check_owner` and `check_group_leader` return `OwnerCheck` (ALIVE, GONE, REPLACED, UNKNOWN), computed per call and never stored. Each caller maps it to keep, reclaim or signal. Every per-script copy of a start reader is deleted.

**Files:**
- `scripts/lib/process_start.py` — the shared reader and `OwnerCheck`; `scripts/lib/test_process_start.py`.
- `scripts/lib/process_start_test_support.py` — the one fake stat helper for tests (`write_stat`, `process_start_probe`, `stored_start_probe`).
- `scripts/direct/assign.py`, `findings.py`, `verify.sh`, `end_session.sh` — the five rerun fixes; tests in `test_assign_launcher.py`, `test_findings.py`, `test_verify_hold_lint.py`, `test_verify_hold_wait.py`, `test_end_session.py`.
- Callers of the shared reader (own copy deleted): `scripts/direct/` (`step_record.py`, `tree_watch.py`, `board.sh`, `implement.sh`), `scripts/production/` (`retire_unit.py`, `merge_gate.py`, `fake_unit_host.py`, `rename_unit.py`), `scripts/herdr/herdr.py`, `scripts/message/sessions.py`, `scripts/agents/` (`remote_control.py`, `stop.py`, `inventory.py`, `pane_record.py`), `scripts/nix_env/heal.py`, `scripts/mac_test/` (`mac_test.py`, `copy_watch.py`), `scripts/build_targets/build_targets.py`, `scripts/build_hold/` (`build_hold.py`, `hold_enforcer.py`), `scripts/lint/` (`memory_admit.py`, `tmp_sweep.py`), `scripts/buildlog/mac_idle.py`. Their tests and test support modules use the shared fake stat helper.

**Binds later work:**
- Any code that reads a process start or decides a process is stale calls `scripts.lib.process_start` (`check_owner`, `check_group_leader`). Only GONE and REPLACED reclaim. Only ALIVE signals. UNKNOWN keeps the record.
- Tests fake process state through `scripts/lib/process_start_test_support.py`, never a local stat writer.
- "A stopped verify.sh wait is found and closed on a rerun" uses the staged wait-file publication in `verify.sh` and the shared reader for its owner check.

**Gotchas:**
- A Mac start time is `ps -o lstart=` text, read with `LC_ALL=C` and `TZ=UTC`. A host without /proc gives UNKNOWN on a date mismatch, never REPLACED.
- A test that patches a per-script start reader breaks, because those readers are gone. Patch `scripts.lib.process_start`.
- A stop inside `phase_wait_start` still leaves an open wait with no wait file. "A stopped verify.sh wait is found and closed on a rerun" closes it.

### Phase 18 — The Stop hook lets a full-context stop through only when something live will wake the session  · status: done
**Depends on:** none (showrunner fix, 2026-10-29: ahead of other work)

#### As-built

- `scripts/hooks/stop-direct-continue.py` measures context before any wake-source check. Below the handoff threshold, or when context cannot be measured, the stop passes.
- At or above the threshold, the only wake source is a live entry in the payload's `background_tasks` (`pending_background_tasks`). With none, the hook blocks once, so the next request runs compaction. The `heartbeat.log` age is not a wake source: a finished worker writes the heartbeat too. `workers_working` no longer exists in `direct_run.py`.
- The other early returns keep their meaning: a subagent, no session id, no active run, a declared user or supplier wait (`record_turn_wait`), and `stop_hook_active` (block once only).

**Files:**
- `scripts/hooks/stop-direct-continue.py` — context first, then background tasks
- `scripts/hooks/direct_run.py` — `workers_working` removed
- `scripts/hooks/test_stop_direct_continue.py` — runs the real hook once per return in `main()`, the finished-heartbeat block case included

**Ruled out:** a fresh `heartbeat.log` as proof of a later wake-up.

### Phase 19 — An uncertain wait reason prints once, with its count  · status: done
**Depends on:** none (showrunner add-on from hierarchies-unit, 2026-10-29)

#### As-built

- `progress_history.py waits` writes an `UncertainCoverage` phase's reason through `_counted_uncertain_reasons`: each distinct uncertain-wait reason once, in first-seen order, joined with "; ". A repeated reason gets " (<n> times)" after it; a reason seen once has no count. `candidates` and `attribution_miss_ids` are unchanged.

**Files:**
- `scripts/direct/progress_history.py` — `_counted_uncertain_reasons`
- `scripts/direct/test_progress_history.py` — the repeated-reason test

### Phase 20 — A stopped verify.sh wait is found and closed on a rerun  · status: done

**Depends on:** Phase 17 (its wait publication in `verify.sh`)

#### As-built

- `progress_history.py` `publish_wait` is the one recorder-owned wait publication: it makes the wait id, writes the wait file, then appends `wait_started`. `verify.sh` `phase_wait_start` and `record_turn_wait` call it. A turn wait checks and replaces its marker and appends the start under one recorder lock.
- `progress_history.py recover-wait-folder` scans a hold folder, closes its markers and renames it to `.verify-hold-removing.<uuid>`, all under the run lock. Recursive deletion runs only on the renamed path. `verify.sh` retries deletion-only folders in a separate pass with no owner check; `phase_wait_remove_directory` is gone.
- History reads return `RecordedWait`, `NeverStartedWait` or `WaitHistoryUnavailable`. `finish-wait` exits 4 for a never-started id and 5 for an abandoned id; `verify.sh` removes the wait file only on exit 0 or 4 and otherwise keeps it with a diagnostic.
- A WaitIdFile commit rechecks under the append lock that the wait file still names its id and that the phase attribution is current; on a mismatch it records the id abandoned (exit 5) and leaves no open wait.
- `finish_turn_wait` handles unreadable history at the library boundary: it keeps the marker, prints to stderr and returns, so the prompt hook's other work continues.
- `scripts/lib/atomic_file.py` `write_text_atomic(..., create_parent: bool = True)` takes `create_parent` keyword-only; the wait publishers and `copy_watch.py` pass False. The copied atomic writer in `progress_history.py` is gone.

**Files:**
- `scripts/direct/progress_history.py` — `publish_wait`, `recover-wait-folder`, `finish-wait` outcomes, typed history reads
- `scripts/direct/verify.sh` — calls `publish_wait` and `recover-wait-folder`; deletion-only retry pass
- `scripts/lib/atomic_file.py` — shared `write_text_atomic` with keyword-only `create_parent`
- `scripts/mac_test/copy_watch.py` — result publication needs the pre-created hold folder and never recreates it
- `scripts/direct/verify_hold_wait_test_support.py`, `test_verify_hold_wait.py`, `test_progress_history.py` — stop-and-resume interleaving regressions on the shared fixture

**Binds later work:** every wait publisher calls `publish_wait`. There is no second wait-file writer or id format. Freeze the recorder, launchers and wait records before each comparison sample. The deleted mesh producer creates no remaining dependency.

**Gotchas:**
- Deleting a folder by its original path is never safe while a publisher knows that path; only a rename under the lock makes a late publisher fail.
- A shared writer that creates a missing parent reopens the late-publication race; publishers into a hold folder pass `create_parent=False`.

### Phase 21 — repair frees a phase record whose reservation a sibling's checkpoint released  · status: done

**Depends on:** none

#### As-built

- `phase_reservation.py repair` frees an `Active` record whose reservation berth's board shows in `CHECKPOINT_RELEASED` (`outstanding`, `released_after_checkpoint`) when no live mapped reservation restores `Active`. A sibling phase's checkpoint can release it through a shared `reservation_shares` group.
- The reset writes `{"kind": "enrolled_awaiting_first_touch", "phase": <phase>}`, keeps the saved board answer and the baselines, and prints the `claim` command (exit 0).
- Order: the repairs log line (reservation id, board status, new state) and the board line come first; the state write comes last, so a rerun finishes a failed attempt. A failed board post exits 1 and leaves the record unchanged.
- An unreadable board answer, for the recorded or a mapped reservation, keeps the record unchanged (exit 2). A record with a release marker or a pending release, or a reservation in any other status, keeps the earlier behaviour.
- `_repair_refused` on a record in `HOLDS_RESERVATION` prints "<reason>; record unchanged" and no `enroll` command. `_post_repair(..., record_changed=False)` says "board post failed; record unchanged".

**Files:**
- `scripts/direct/phase_reservation.py` — `_repair_command`, `_repair_refused`, `_post_repair`
- `scripts/direct/test_phase_reservation.py` — the reset and keep-files tests; the fake `cargo-berth` answers `board` and `check`
- `docs/direct/phase_reservation.md` — row "Lost open phase record" and the `repair` paragraph

**Gotchas:** `repair` and `committed` take no shared lock, so a checkpoint during a repair of the same phase can lose its pending-release record. The cargo-berth side of the shared release belongs to tile.

**Ruled out:** a repair/checkpoint lock in this phase (a new guard; a next item).

### Phase 22 — Checkpoint notices carry no Parallel line  · status: done

**Depends on:** none

#### As-built

- Checkpoint notices carry no `Parallel:` line. `merge_checkpoint.py` takes no `--parallel` argument and has no `check_parallel`.
- `phase_table.py` has no `parallel` subcommand, no `"parallel"` JSON key and no `CouldStart`, `NothingToStart`, `DependenciesUnknown` or `ParallelCheck` types.
- `plan_history.plan_phases(plan_path)` reads no `**Depends on:**` line; each `PlanPhase` has `id`, `title` and `done`. `plan_member` stays for `phase_table.py` and `progress_history.py`.
- `dailies_input.py` prints no `parallel check:` line.
- The docs keep the `**Depends on:**` rule and the rule that a director starts independent phases itself, with no report line.

**Files:**
- `scripts/production/merge_checkpoint.py` — checkpoint merge request checks, without the Parallel line
- `scripts/direct/phase_table.py` — phase table build and render
- `scripts/direct/plan_history.py` — plan phase reader
- `scripts/production/dailies_input.py`, `scripts/production/unit_phases.py` — callers without the parallel data
- `docs/production_format.md`, `commands/showrunner/produce.md`, `commands/unit/checkpoint.md`, `docs/direct/phase_end.md`, `commands/plan/to_phased_plan.md`, `docs/as-built/phase-tables.md`, `docs/as-built/showrunner-automation.md` — notice format and phase-start rules

**Ruled out:** a replacement check for independent phases (user order).

### Phase 23 — Transcript lookups do not resolve each file  · status: done

**Depends on:** none

#### As-built

- `transcript_paths(root, pattern)` calls no `resolve()` on each file. It keys each match by `uncompressed_path(path)`, so a plain file and its `.zst` copy give one entry, with the plain file preferred.
- `distinct_files(paths)` in `scripts/lib/transcripts.py` merges symlink aliases in a small list of matches. A caller that counts or picks among the matches of one session uses it: `read_director_transcripts` in `phase_process.py`.
- `showrunner_transcripts` in `nightly_review.py` resolves each path as it enters its result set, so a recorded path and a discovered path give one entry.
- `_found_backends` in `staff.py` lists each real session folder once and reuses the list for each home that links to it. A row gets `CodexBackend(home)` only for its recorded home.
- Paired CPU per call on natedev, 662 rows: `_found_backends` 0.08–0.10 s against 0.45–0.52 s before; `transcript_paths` over three homes 0.16 s against 0.42 s.

**Files:**
- `scripts/lib/transcripts.py` — transcript discovery and `distinct_files`
- `scripts/production/staff.py` — Codex backend lookup, one scan for each real folder
- `scripts/direct/phase_process.py`, `scripts/nightly_review/nightly_review.py` — callers that merge aliases
- `scripts/lib/test_transcripts.py`, `scripts/production/test_staff.py`, `scripts/direct/test_phase_process.py` — alias, shared-folder and plain-preferred rows

### Phase 24 — Phase start names an unclosed phase  · status: done

**Depends on:** none

#### As-built

- When `start-phase` sets aside the current phase and no open pass belongs to it, `_report_unclosed_phase` prints one line to stderr: `anomaly: start-phase: <unit>: phase <id> set aside unclosed, started <YYYY-MM-DD HH:MM zone>`. It stops nothing.
- A set-aside phase with an open pass prints no line. A run with no unit row names the unit as `unit not recorded (<reason>)`.

**Files:**
- `scripts/direct/progress_history.py` — `_report_unclosed_phase` and its call in `_start_phase`
- `scripts/direct/test_progress_history.py` — rows in `test_a_phase_started_while_another_is_open_leaves_the_older_one_open`

### Phase 25 — A reservation released without a checkpoint clears  · status: done

**Depends on:** none

#### As-built

- `phase_reservation.clear` deletes an `Active` record when berth's board in `repo` shows its reservation `released_without_checkpoint` (`RELEASED_WITHOUT_CHECKPOINT`). That clear writes no release marker.
- Every other `Active` lifecycle (`active`, `outstanding`, unreadable) stays refused, as before.

**Files:**
- `scripts/direct/phase_reservation.py` — `clear`
- `scripts/direct/test_phase_reservation.py` — rows in `test_a_held_reservation_is_never_replaced_or_cleared_by_hand`, board answer patched

### Phase 26 — Record-active ignores paths a merge brought  · status: done

**Depends on:** none

#### As-built

- `phase_reservation.changed_since_baseline` lists the paths of the branch's first-parent commits from the baseline to `HEAD`, the uncommitted changes against `HEAD`, and the untracked files.
- With `--diff-merges=dense-combined`, a merge commit adds only a path its result changes from every parent, as a conflict resolved by hand. A plain merge adds nothing, at phase start or later.
- A path the phase changed and later changed back still counts.

**Files:**
- `scripts/direct/phase_reservation.py` — `changed_since_baseline` and the module docstring
- `scripts/direct/test_phase_reservation.py` — `test_record_active_claims_only_the_paths_a_merge_resolved_by_hand`

### Phase 27 — Stall-watch sees a permission prompt on the screen  · status: done

**Depends on:** none

#### As-built

- `stall_watch.prompt_on_screen` finds a `PROMPT_QUESTION` line with a `PROMPT_OPTION` answer (`❯ 1. Yes`) under it in the pane's bottom `PROMPT_LINES` lines.
- `watch_pass` treats a pane that shows one as blocked, whatever herdr's state is. The pass sends no bump and alerts on the usual prompt timing from the pending call's time.
- rca 261114-1 cause: herdr reads the dialog as idle when an input box shows under it (`live_prompt_box`), so stall-watch saw the prompt only when herdr first reported it blocked.

**Files:**
- `scripts/production/stall_watch.py` — `prompt_on_screen`, `PROMPT_OPTION` and the blocked decision in `watch_pass`
- `scripts/production/test_stall_watch.py` — `test_a_prompt_herdr_reports_idle_is_blocked_on_the_first_pass`; four tests that end a prompt clear it from the screen

### Phase 28 — An accepted finding takes the user's decision after close  · status: done

**Depends on:** none

#### As-built

- `_decision` takes a held, decided or accepted finding, also after the phase ledger closes. Other states keep the refusal.
- An accepted finding keeps its `accepted` state, because a reviewer already accepted its repair.
- A ruling is the saved `decision`, whatever the state. `--withdraw` and the repeat rule use that test.
- Case: tile phase 62 F003 (accepted, ledger closed) refused the user's "Approved". tile records it with `decision --phase 62` after the fix reaches main.

**Files:**
- `scripts/direct/findings.py` — `_decision`
- `scripts/direct/test_findings.py` — `test_an_accepted_finding_in_a_closed_phase_takes_the_users_decision`, `test_a_decided_finding_a_verdict_accepted_changes_its_ruling_with_withdraw`
- `docs/as-built/simplify-lens.md` — the decision paragraph

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

#### As-built

**Measure now** uses the Lucide `refresh-cw` icon and posts `force=1`. The page shows `Paused · Measuring…` and disables the button until the data reaches its target, the wait expires, or the request fails.
`start_measure(now, launch=launch_measure, *, force=False) -> MeasureRequest` skips the cache when forced and returns the active target if a measure holds the lock.
`disks.coord` protects reservation and publication, and `disks.active` records the active generation. The launcher passes the locked descriptor to the child, which holds it through publication. The child sets `measured_at` at publication and removes `disks.active` before it releases the lock.

**Files:**
- `scripts/disks/disks_page.py` and `scripts/disks/disks.html` — measure reservation, button rendering, and generation checks.
- `scripts/disks/measure.py` and `scripts/lib/file_lock.py` — child reservation and `ExclusiveLock` ownership.
- `scripts/review_links/serve.py` and `scripts/review_links/page_frame.py` — the POST returns `state`, `target`, `slots`, and `sessions`. `createPageRefresh()` exposes `setStatus()`.
- `scripts/disks/test_disks_page.py`, `scripts/disks/test_measure.py`, `scripts/lib/test_file_lock.py`, `scripts/review_links/test_page_frame.py`, and `scripts/review_links/test_serve.py` — generation, lock handoff, and browser or route tests.
- `docs/plans/builds-report.md` — the Disks generation completion rule.

**Gotchas:** `ExclusiveLock.close()` closes one handle. `release()` unlocks the inherited lock for both processes. Legacy data has generation zero.

**Ruled out:** Retrying a measure request with an unknown target.

### Phase 46 — A question title is one short line, and the Review page never shows a long one  · status: done

#### As-built

`check_question_title(text) -> ValidQuestionTitle | RefusedQuestionTitle` requires one nonempty line within `question_title_max_chars`, which is 100 by default. `ask` checks `--text` and `--ask` before review, and `write_question_record()` checks new or changed titles.
`questions --state` refuses invalid new titles and reports `reword` for stored titles that it keeps.
The Review table clips the first line by character count and available width. Search and the tooltip retain the full title. The details panel shows its hidden Item fallback when the rendered body lacks the full title.

**Files:**
- `config/production.conf` and `scripts/production/update_registration.py` — the limit and title validation.
- `scripts/review_links/review_page.py` — one-line rows and the full-title fallback.
- `docs/question_template.md` and `commands/showrunner/produce.md` — the title rule and the use of `--context` and `--item`.
- `scripts/production/test_update_registration_questions.py` and `scripts/review_links/test_review_page.py` — filing and rendering tests.

**Gotchas:** CLI help names the setting. The CLI reads its value when it checks a title. The details check preserves block boundaries in the text.

### Phase 47 — A rename of a unit to its own name keeps bare worker names quiet  · status: done

#### As-built

`rename_qualified_workers(owner)` matches a prefix equal to the current owner name or ending in `-<owner>`. It keeps other names without per-row output or rename hints.
`rename_plan()` treats the current unit name plus `-unit` as a same-name rename. `rename_apply_unit()` can remove an unstarted pending rename under the staff row lock.
`rename_unstarted(record, row, pending) -> bool` requires the staff path and checkout at the original source, the original branch, and an absent destination. Any native Git worktree lock makes that test fail.

**Files:**
- `scripts/agents/shutdown.py` — name matching, summary output, and pending rename replacement.
- `scripts/production/unit_move.py` — the unstarted move check.
- `scripts/agents/test_shutdown_cli.py` and `scripts/production/test_unit_move.py` — rename and move fixtures.

**Binds later work:** `rename_qualified_workers()` in `scripts/agents/shutdown.py` prints `<N> worker names unchanged: no qualified prefix for <owner>` once on stderr after the scan when the count is positive. **A rename summary for one worker says "1 worker name unchanged"** uses this summary.

**Gotchas:** Staff rows store no former names. The session record’s `formerNames` is process-specific, so qualification uses the current owner name.

### Phase 48 — An image can carry a question, and its answer comes back to the user  · status: done

#### As-built

**Ask question** uses the image card’s feedback field and sends the image owner the question and an answer command. The card stays visible while any question is open, and the image leaves the console’s waiting set.
`images.py answer --id <image> --question Q<n> --text "<answer>"` saves the answer and clears `feedback_at` in one write. The console marks the image new after the last answer.
Each item assigns its own `Q<n>` IDs. The store refuses unknown or already answered IDs and reads older records without a `questions` field.
Images refreshes after each action and polls during question delivery. Images and Review restore focus, caret, and selection after a refresh.

**Files:**
- `scripts/review_links/page_question.py` — `QuestionStore` defines `questions(item)`, `add(item, question)`, and `set_answer(item, question_id, answer)`. `ask(store, item, owner, source_path, row, title, words, answer_command, status) -> MessageDelivery` saves and claims delivery. `answer(store, item, question_id, text) -> Answered | AnswerRefused` records a reply for the page renderers.
- `scripts/review_links/question_record.py` and `scripts/review_links/image_store.py` — typed question records, optional image questions, and locked image writes.
- `scripts/review_links/images.py`, `scripts/review_links/serve.py`, and `scripts/review_links/change_page.py` — image actions and the delivery claim. The POST responds before delivery.
- `scripts/review_links/page_frame.py` and `scripts/review_links/review_page.py` — shared focus restoration.
- `commands/show_image.md` — the answer command.
- `scripts/review_links/test_page_question.py`, `scripts/review_links/test_images.py`, `scripts/review_links/test_review_page.py`, `scripts/review_links/test_change_page.py`, and `scripts/review_links/test_serve.py` — storage, delivery, route, and refresh fixtures.

**Gotchas:** An answer that arrives during an earlier feedback send takes priority. An open question keeps the card visible even when an older feedback mark exists.

### Phase 49 — The fast-track recovery reads each Git object once, in one batch  · status: dropped
**Result (2026-11-17):** dropped with no code change. test_fast_track makes 7,185 Git starts, and only 112 are cat-file. The batch saved no CPU (61.934 s to 61.910 s). No single call shape dominates.

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

#### As-built

On `origin/main`, `append_rows()` seeds each `To review` column with an empty string before it applies the new row’s values.
The rule covers `add`, `list-ready`, and `live`, including tables with `Review log` or other optional columns.
`_row(path, columns, values)` rejects a missing table value and writes only columns present in the table.

**Files:**
- `scripts/production/change_review.py` — row defaults and strict column formatting.
- `scripts/production/test_change_review.py` — insertion with extra columns and without an Image column.

**Gotchas:** The phases checkout lacks the `To review` defaults present on `origin/main`.

### Phase 51 — An image question never reads as unsent when it was sent  · status: done

#### As-built

`ReviewLinksServer` sets the startup recovery flag before recovery starts and clears it when the delivery scan finishes. Images shows that a pending question is sending and polls before recovery claims it.
The sender replaces the intent with a `started` mark before sending. It saves `accepted` before it writes the delivery result.
An unknown result offers a deliberate resend with the same words and question ID. If the intent update fails after the question saves, cleanup clears the intent and leaves the question eligible for retry.

**Files:**
- `scripts/review_links/change_page.py` — dispatch marks and the live attempt check.
- `scripts/review_links/serve.py` and `scripts/review_links/images.py` — the recovery flag, delivery notices, and polling.
- `scripts/review_links/page_question.py` — retry claims and cleanup after an intent write failure.
- `scripts/review_links/test_images.py` and `scripts/review_links/test_page_question.py` — startup, write failure, and resend fixtures.

**Gotchas:** Legacy intents without a send mark still use startup recovery. An intent from an older sender can be ambiguous after a failed result write.

### Phase 52 — A rename to a name the folder and branch already carry prints no anomaly  · status: done

#### As-built

`_unit_rename_target(value, old_name, new_name) -> str | None` uses `unit_names.swap_stem()` to replace a trailing old-name suffix first.
If only the new-name suffix matches, the folder or branch keeps its value without an anomaly. Any value with neither suffix still produces its own anomaly.

**Files:**
- `scripts/agents/shutdown.py` — the shared target rule for implicit folder and branch names.
- `scripts/agents/test_shutdown_cli.py` and `scripts/production/test_unit_move.py` — already-matching targets and anomaly tests.

### Phase 53 — The image question code has no import cycle, and page_question has its staff accessor row  · status: done

#### As-built

`question_record.py` defines `Question(question_id, asked_at, words, answer=None)`, `Answer(text, answered_at)`, their stored JSON forms, and the `AnswerRefused` exception. `stored(question) -> StoredQuestion` encodes the record. `read(value) -> Question` validates the ID, timestamp, words, and optional answer.
`image_store.py` imports those types from the page-independent module. `page_question.py` retains the store protocol, delivery API, renderers, and answer result types.
The staff accessor table records `page_question.py` as a read accessor for the recipient’s `StaffId`.
`change_review.py` checks question exchanges through `delivery_state.py`, which keeps delivery checks below the page layer.

**Files:**
- `scripts/review_links/question_record.py` — the shared record types, exception, and JSON conversion.
- `scripts/review_links/image_store.py` and `scripts/review_links/page_question.py` — the store and page operations.
- `scripts/review_links/change_page.py`, `scripts/review_links/change_browser.py`, `scripts/review_links/images.py`, `scripts/review_links/investigations_page.py`, `scripts/review_links/list_page.py`, `scripts/review_links/review_actions.py`, `scripts/review_links/review_page.py`, and `scripts/review_links/serve.py` — direct exception or record imports.
- `scripts/production/change_review.py` and `scripts/production/delivery_state.py` — question exchange checks and their shared location.
- `docs/as-built/staff-accessors.md` — the `StaffId` accessor entry.

### Phase 54 — One fast-track call finds each path and ref once  · status: dropped
**Result (2026-11-17):** dropped with no code change. One fetch under the landing lock and a reused worktree root cut Git starts by 2.62 percent (7,185 to 6,997); CPU rose from 56.4 s to 60.2 s. Below the 20 percent rule.

**Depends on:** phase 53, phase 51 (order only); natest phase 323 (a0463a3b9) merged to main if test_fast_track.py changes

#### Work Order

**Goal:** the showrunner with builds (2026-11-17 14:40 EST): "remove repeated path and ref discovery inside one fast-track call (the gain counts for each real fast-track call too)." test_fast_track makes 7,185 Git starts on bab921f45. The top shapes are `rev-parse --path-format=absolute` 476, `fetch origin` 383, `rev-parse --show-toplevel` 350, `rev-parse HEAD` 303, `remote get-url` 156.

**Spec:**
1. Measure first with a PATH `git` shim that logs argv: Git starts by shape, CPU and wall time of `production/test_fast_track.py`, on the base.
2. In one fast-track call, find the top-level folder, the common Git folder and the `origin` URL once, and pass them down. Fetch `origin` once for each step that needs a fresh ref.
3. Keep each read of a mutable ref where its value can change inside the call. Keep every write as it is.
4. The behavior of `reconcile`, `push_patch`, `restore_prepared` and `land_main.land` does not change.
5. Out of scope: which tests need the full merge flow.
6. natest owns `test_fast_track.py`. Change no line of it unless the base is main after natest phase 323 merges.

**Files:**
- `scripts/production/fast_track.py`
- `scripts/lib/land_main.py` and the copy and slot helpers only where they repeat the same discovery inside one call

**Workers:** 1 writer.

**State:** none.

**Acceptance gate:**
1. `scripts.production.test_fast_track` passes.
2. basedpyright reports 0 errors and 0 warnings on `scripts/production` and `scripts/lib`.
3. The shim measure before and after shows 20 percent or more fewer Git starts and a CPU gain. If not, close with no change.
4. The checkpoint carries the measured numbers in one line for builds.

### Phase 55 — An image question card always says when a resend is needed  · status: done

#### As-built

`ImageQuestionDispatchState` records `started`, `started_after_unconfirmed`, or `accepted`. A refused resend after an unknown result keeps the earlier result unknown. Acceptance clears the notice.
An explicit `saved: false` on a valid delivery record supplies the retry notice when the question has no active intent. An invalid saved-message record gives no resend notice and leaves the Images page available.

**Files:**
- `scripts/review_links/change_page.py` — dispatch state, failure selection, and validated reads of image question metadata.
- `scripts/review_links/images.py` — the resend notice for an explicit unsaved intent.
- `scripts/review_links/test_images.py` — refused resends, intent write failure, and invalid metadata fixtures.

### Phase 56 — The timed-refresh browser tests pass under load  · status: done

#### As-built

The Images browser probes run the refresh callback scheduled by the page and wait for a named DOM condition through `MutationObserver`. `controlRefresh()` captures only the delivery refresh interval and advances the probe clock by its configured period.
A tick that meets an active refresh tries again on the next animation frame.
The test server joins its request handlers before fixture cleanup and gives each handler socket a five-second timeout.

**Files:**
- `scripts/review_links/test_images.py` — `question_probe()` and `run_question_browser()` cover focus, selection, and recovery after a failed refresh.

**Gotchas:** The fixture uses a five-second refresh period and a 12-second limit per condition. A delayed refresh timer reproduces the timeout. CPU load alone does not establish the cause. Shared worktrees can cause separate fixture config and socket errors.

### Phase 57 — A rename summary for one worker says "1 worker name unchanged"  · status: done

#### As-built

`rename_qualified_workers(owner)` in `scripts/agents/shutdown.py` prints `<N> worker name unchanged: no qualified prefix for <owner>` on stderr for a count of one, and `worker names` for any other positive count.

**Files:**
- `scripts/agents/shutdown.py` — the summary line.
- `scripts/agents/test_shutdown_cli.py`, `scripts/production/test_unit_move.py` — the singular and plural summary assertions.

### Phase 58 — Every validate message carries its validation id and command  · status: done
**Result (2026-11-17):** the C368–C370 validate messages came at 10:55 EST from a server that ran code from before de367d2b2, whose formatter had no id line. C368 and C369 saved before the id check shipped; C370's verdict came after it. de367d2b2 already sends the id and the full command on the first send and on recovery. This phase adds the test assertions for both, which fail on the old formatter, and the Validate section of `commands/change_review.md` points to the command in the message.

**Depends on:** Phase 57 (order only)

#### Work Order

**Goal:** the sdlc showrunner (2026-11-17), from the cargo-liner showrunner: the C370 verdict was refused with "this question id does not match the current question". It needed `--validation <attempt>`, but its "the user asks you to validate this" message did not carry the id. C368 and C369, from the same minute, saved without `--validation`. The sdlc validate messages C1839 and C1844 did carry "Validation id:" and the command.

**Spec:**
1. Find the code path that sent C370's validate message without "Validation id:" and the command (`scripts/review_links/change_page.py:1216-1218` builds the id line).
2. Find why C368 and C369 saved without `--validation`, and state whether that is right.
3. Every validate message carries the validation id and the full verdict command with `--validation`.
4. The existing validate message test covers the case.
5. `commands/change_review.md` (Validate) names `--validation <id>` and says the id is in the validate message.

**Files:**
- `scripts/review_links/change_page.py`
- `scripts/review_links/test_change_page.py`
- `scripts/production/change_review.py`, `scripts/production/test_change_review.py` if the cause is there
- `commands/change_review.md`

**Workers:** 1 writer (`design`).

**State:** the delivery slots and validation records the change writes.

**Acceptance gate:**
1. basedpyright on every changed Python file: 0 errors, 0 warnings.
2. The changed modules' suites pass.
3. The new check fails without the fix.

### Phase 59 — A short-lived vault index lock retries and asks no question  · status: done
**Depends on:** none

**Result:** a held vault index lock retries each hour and files the vault_locked question only after the same lock stays past `vault.lock_question_age_s` (new key, 7200 s). The Mac `stat` gives device, inode and birth time; a new lock starts a new age, and a lock with no identity ages from its first stop. 47 vault tests pass; basedpyright clean.

#### Work Order

**Goal:** the sdlc showrunner (2026-11-17), from a natedev finding: `scripts/buildlog/vault_pull.py` on the Mac hit "index.lock: File exists" on 11-16 16:11 EST and 11-17 16:17 EST and filed a user question each time. The lock was transient: another Git process in the vault held it. When the index lock is held, wait and retry. File the vault_locked question only when the same lock stays past a set age.

**Spec:**
1. Find why both runs filed a question: the in-script retry (`vault.lock_retries`, `vault.lock_retry_delay_s`) and the second-stop rule in `_vault_locked`. Use the vault state and the message log; read only.
2. A held index lock gives a retry, not a question. The run reports the lock and the next run tries again.
3. File the vault_locked question only when the same lock (the same index.lock file, by its modification time or another stable identity the remote script can print) stays past a set age. A new lock starts a new age.
4. The age is a new key in `config/build_targets.conf`, with a range comment like its neighbours. The retry pause uses the existing `vault.lock_retry_delay_s`, or a new key if its meaning changes; do not change an existing key's meaning or range.
5. Tests write a fixture config; no test reads the live value. Add the cases to the existing vault lock tests.

**Files:**
- `scripts/buildlog/vault_pull.py`
- `scripts/buildlog/test_vault_pull.py`
- `config/build_targets.conf`

**Workers:** 1 writer (`design`).

**State:** the vault state file and its outage record.

**Acceptance gate:**
1. basedpyright on the changed Python files: 0 errors, 0 warnings.
2. `scripts.buildlog.test_vault_pull` passes.
3. The new checks fail without the fix.

### Phase 60 — A failed write or Git launch keeps saved review work  · status: done
**Depends on:** none

**Result:** five failure paths keep saved work. A Git launch error after a saved verdict is a commit warning; the verdict and its claim stay. A failed delivery metadata write leaves a private receipt, and startup recovery sends the saved message once or keeps it. Later saves the deadline before its log and restores it if the log fails. The nightly writer and Review share one ledger lock. A saved efficiency Decline stays a success when the nightly summary fails.

#### Work Order

**Goal:** the sdlc showrunner (2026-11-17), from the admin-console reviewscroll reviews: four failure paths on main lose or misstate saved work when a disk write or a Git launch fails. Each failure must leave saved state true and the user told the truth.

**Spec:**
1. Change review verdict: `scripts/production/change_review.py` `_edit` (about line 566) writes the verdict, then `_commit` launches Git. An `OSError` from the Git launch escapes, and `scripts/review_links/change_page.py` (about line 1054) clears the delivery claim and returns 400. The saved verdict must stand, and the claim must not clear.
2. Delivery metadata: `scripts/review_links/review_actions.py` (about line 202) leaves the delivery file at `saved: false` when the write after the save fails. Startup recovery in `change_page.py` (about line 1277) then drops the saved message. The message must not be lost.
3. Later: `review_actions.py` `later()` writes the source log before `review_later.save` writes the deadline. A deadline write failure leaves a false log entry, and the request takes the 503 path (`serve.py` about line 671). Order the writes so that a failure leaves no false log entry.
4. Nightly ledger: the nightly CLI efficiency ledger writer does not take the Review store write lock that Review saves use (`scripts/review_links/efficiency_reviews.py`). Make it share that one lock.

**Files:**
- `scripts/production/change_review.py`, `scripts/production/test_change_review.py`
- `scripts/review_links/change_page.py`, `review_actions.py`, `review_later.py`, `serve.py`, `efficiency_reviews.py`
- their tests: `test_change_page.py`, `test_review_actions.py`, `test_serve.py`, `test_review_page.py`
- the nightly ledger writer under `scripts/nightly_review/` and its test

**Workers:** 1 writer (`design`).

**Order:** admin-console reviewscroll edits the same files and merges first. Build on main 121a03c90; merge the reviewscroll hash (about 22:00 EST) before this phase commits.

**Acceptance gate:**
1. A fault-injection test for each item fails without the fix.
2. basedpyright on the changed Python files: 0 errors, 0 warnings.
3. The suites of the changed modules pass.

### Phase 61 — The Disks page refresh button shows on the live page  · status: done
**Depends on:** none

**Result:** the live natedev page held the button as a faint 20 px icon at the far right, easy to miss. The button now shows the words Measure now in a bordered box. The Mac console publishes the page on each full console load; no code defect there. A browser check covers the button in every status state.

#### Work Order

**Goal:** the user (2026-11-17 19:47, change review C1850) on the Disks page refresh button from phase 45: "not works: it's not here". The live published page `~/.local/share/review-links/ysbjmg/disks.html` holds `<button id="measure-now">` inside the status line, and live main 121a03c90 has the phase 45 code. The user still sees no button.

**Spec:**
1. Open the live Disks page in headless Firefox at desktop width, dark mode, from the console server (http://100.103.14.30:8765, the console's Disks link). Take a screenshot of the status line. Read only; do not press the button.
2. Find why the button does not show: for example, status code that rewrites `.page-status-state`, a style that hides or clips it, or a page the console links that is not the published one.
3. Fix it where the cause is. The button shows at the right of the status line in every status state, and it stays after each refresh.
4. Add a check that fails without the fix: a browser check if the cause is in the page script, or a table row in the existing Disks page test.

**Files:**
- `scripts/disks/disks.html`, `scripts/disks/disks_page.py`, `scripts/disks/test_disks_page.py`
- `scripts/review_links/page_frame.py`, `scripts/review_links/test_page_frame.py`

**Workers:** 1 writer (`design`).

**Acceptance gate:**
1. A dark desktop screenshot of the fixed page, clipped to the status line, shows the button. A screenshot of the live page shows the cause.
2. The new check fails without the fix.
3. basedpyright on the changed Python files: 0 errors, 0 warnings. The Disks and page frame suites pass.

### Phase 62 — Image answer buttons use the Review page groups  · status: done
**Depends on:** none

**Result:** `page_frame.review_action_groups` and one shared style render the labeled button groups on the Review and Images pages. Images: Approved, Not Approved and Send Feedback under Closes the item; Ask question under The item returns to you, after a separator bar. Phones stack the groups with no bar.

#### Work Order

**Goal:** the user (2026-11-17 19:56, change review C1854) on the Images page answer buttons: "it is there but i think that ou should use the same affordance you do here in the review - the text above that says "CLOSES THIS ITEM" a separator bar and a "THE ITEM RETURNS TO YOU" - i do see the button".

**Spec:**
1. The Images card groups its answer buttons as the Review page does (`review_page.py` `form`, the `review-action-group` markup and style): a small upper-case label above each group, and a separator bar between groups.
2. Buttons that close the image item go under "Closes the item". Ask question goes under "The item returns to you". Read the Images code to place each button by what it does.
3. One shared helper and one shared style make the groups for both pages. No copied markup or style.
4. The phone layout keeps the Review page rule: groups stack with no bar.

**Files:**
- `scripts/review_links/images.py`, `scripts/review_links/review_page.py`, the shared module for the helper (for example `scripts/review_links/change_browser.py` or `page_frame.py`)
- `scripts/review_links/test_images.py`, `scripts/review_links/test_review_page.py`

**Workers:** 1 writer (`review4`).

**Order:** admin-console reviewscroll edits `review_page.py` and `change_browser.py` and merges first. Keep the `review_page.py` edit to the call of the shared helper.

**Acceptance gate:**
1. One dark desktop shot of an Images card's answer buttons, before and after, clipped to the buttons.
2. The Images and Review page suites pass. basedpyright: 0 errors, 0 warnings.

### Phase 63 — A command in a question shows in a code block with a copy button  · status: done
**Depends on:** none

**Result:** `page_frame.render_code_blocks` splits text into code blocks and text for answers, review text and every question paragraph (ask, context, trailing, choices). A backticked command alone on its line shows in a code block with a copy button.

#### Work Order

**Goal:** the user (2026-11-17 22:4x EST, Review page question card): "why isn't this in a code block with a copy - that is supposed to work". The sdlc showrunner traced it: `change_browser.question_paragraphs` renders question body paragraphs through `change_page.review_text_html`, which never matches `page_frame.CODE_BLOCK`. Only `answer_html`, `item_html` (numbered steps) and `change_page.review_html` make code blocks.

**Spec:**
1. A command alone on its line in any question paragraph (lead, context, trailing, choices text) renders as `page_frame.code_block_html`, with the copy button.
2. One shared function splits text into code blocks and text. `answer_html`, `review_html` and `question_paragraphs` call it; no three copies of the split loop.
3. Test: question text with "...on natedev:\n`git ...`" renders a code block with the copy button. Add it as a row of the existing code block test if one covers the same behavior.

**Files:**
- `scripts/review_links/change_browser.py`, `scripts/review_links/change_page.py`, `scripts/review_links/page_frame.py`
- `scripts/review_links/test_change_page.py`, `scripts/review_links/test_review_page.py`, `scripts/review_links/test_page_frame.py`

**Workers:** 1 writer (`design`).

**Order:** admin-console reviewscroll rewrites these files and merges first (about 23:15 EST). Merge its hash before this phase commits.

**Acceptance gate:**
1. One dark desktop shot of a question card with a command block and its copy button.
2. The new check fails without the fix. The Review page, change page and page frame suites pass. basedpyright: 0 errors, 0 warnings.

### Phase 64 — An Ask never replaces a different open question  · status: done
**Depends on:** 60, 63

**Result:** under the write lock, Ask refuses when an open legacy JSON Question has the same title and asker. It prints `<title>: a legacy question from this asker is already open` and exits 2. The legacy question stays open. Built on the reviewask part A tip.

#### Work Order

**Goal:** the sdlc showrunner (2026-11-17), from the admin-console reviewask reviews: an ordinary Ask can replace a legacy JSON Question that has the same title and asker before migration (`scripts/production/update_registration.py` about line 1573 on main). An open question is lost with no notice.

**Spec:**
1. Fix it at the source: the Ask write never replaces a different open question.
2. A legacy record with the same title and asker is migrated first, or the Ask refuses with a named reason. Choose the path that keeps the open question and gives the asker a clear result.
3. Add a test that fails on main: a legacy JSON Question is open, an ordinary Ask with the same title and asker runs, and the legacy question stays open.

**Files:**
- `scripts/production/update_registration.py`, its test module

**Workers:** 1 writer (`design`).

**Order:** admin-console reviewask changes the same lines. Build on the reviewask tip that admin-console names.

**Acceptance gate:**
1. The new check fails on main.
2. basedpyright: 0 errors, 0 warnings. The update_registration suite passes.

### Phase 65 — A same-tab link shows a just-closed Change  · status: done
**Depends on:** 60, 63

#### Work Order

**Goal:** the sdlc showrunner (2026-11-18), from the admin-console reviewscroll reviews: on main, a same-tab link can leave a just-closed Change hidden behind its saved marker (`scripts/review_links/review_page.py`, the fragment handler and the visibility check).

**Spec:**
1. Find the cause from the admin-console review text and the live code.
2. A link opened in the same tab always shows the Change it names, and a just-closed Change never stays hidden behind its saved marker.
3. Add a browser check that fails on main.

**Files:**
- `scripts/review_links/review_page.py`, `scripts/review_links/test_review_page.py`

**Workers:** 1 writer (`design`).

**Order:** build on the admin-console reviewscroll tip (about 02:00 EST), after phases 60 and 63.

**Acceptance gate:**
1. The new check fails on main.
2. The Review page suite passes. basedpyright: 0 errors, 0 warnings.

**Result:** a Review link, fresh or in the same tab, shows the Change or Question it names, also just after it closes. The jump keeps the filters that match the card. A link that arrives during a verdict save opens its card after the save. On main 78de4e3ec (hash d53e3a4d2, row C1892).

### Phase 66 — An asked image leaves the page until its answer  · status: done
**Depends on:** none

**Result:** Ask question removes the image or pair from the Images page and every count. The card returns when the owner answers, with the question, the answer and all four buttons. A direct link to a waiting image shows one line, with no buttons. A question that did not reach the owner (unsaved intent, failed send or unknown result) keeps its card, in the counts, with the resend notice. One rule (`images.ImageReview.visible`) serves the page and the console count. Images 55 tests and Review 57 tests pass; basedpyright is clean.

#### Work Order

**Goal:** the user (2026-11-18, via the sdlc showrunner), on the Images page question from phase 48: "you implemented the image page different - when i ask a question you keep it on the page - stating that the question was asked - i don't want that - i want it to work liek reviews where it goes awy and only comes back when it has been answered".

**Spec (design settled by the showrunner):**
1. When Ask question saves, the image or pair leaves the Images page and every count at once. No "question asked" card stays.
2. It returns to the page only when the owner saves an answer. The card then shows the question and the answer, with all answer buttons.
3. A direct link to a waiting image shows one line that the owner will answer, with no buttons, as the Review page does for an asked row.
4. The image stays in the store while it waits. `images.py answer` and `images.py delete` are unchanged.
5. Use the Review page's existing code path for the asked state where one exists; no copy.

**Files:**
- `scripts/review_links/images.py`, `scripts/review_links/image_store.py`, `scripts/review_links/review_page.py`, `scripts/review_links/change_browser.py`, `scripts/review_links/page_frame.py`, `scripts/review_links/serve.py`
- `scripts/review_links/test_images.py`, `scripts/review_links/test_review_page.py`

**Workers:** 1 writer (`design`).

**Order:** ahead of phases 60, 64 and 65. It ships alone on origin/main.

**Acceptance gate:**
1. One dark desktop shot of the Images page with a returned card that shows the question and the answer.
2. Tests: Ask question removes the card and its counts; an answer brings it back with the question, the answer and all buttons; a direct link to a waiting image shows the one line and no buttons. Each fails on main.
3. The Images and Review page suites pass. basedpyright: 0 errors, 0 warnings.

### Phase 67 — A unit name that ends in -unit is refused  · status: done
**Depends on:** none

**Result:** `staff.validate_unit_name_for_write` holds the rule. Staff row creation, `staff.rename` and `staff.change_director` call it, and `add_unit`, `agents rename` (plan and apply), retired resume, `promote_showrunner`, `demote_showrunner` and `producer_transfer` check it before they change files or sessions. A refusal prints `StaffRefused: unit name '<name>' must not end in -unit` and exits 2. Alias lookups of the `-unit` form and old records that hold such a name still work. Tests use fixture staff files only.

**Gate repair:** the first hash went red at the merge gate. Three test fixtures wrote `-unit` names (`TEST-unit` in test_pane_record, `verify-unit` in test_copies, `renamed-unit` in test_merge_checkpoint_bump). Their names changed; the rule did not.

#### Work Order

**Goal:** the Builds page showed a unit as `organon-unit`, because its staff row kept an old name. The user said "that should be impossible". The showrunner asked whether to refuse such names. The user answered: "yes refuse ending in -unit".

**Spec:**
1. Every command that writes a unit name refuses a name that ends in `-unit`. It prints the reason in one line and exits non-zero.
2. The writers include `add_unit`, `agents rename` (rename apply and plan), `promote_unit`, `transfer` and each resume path that can set a name.
3. The rule lives in one place in staff. Each writer calls it. No copy of the check.
4. A name given as an alias (the `-unit` form of an existing unit) still resolves to its row, as today. Only a write of such a name is refused.
5. A read of an old record with such a name does not fail.

**Files:**
- `scripts/production/staff.py`, `scripts/production/unit_names.py`, `scripts/production/add_unit.py`, `scripts/production/session_staff.py`, `scripts/production/staff_move.py`
- `scripts/agents/shutdown.py`, `scripts/agents/producer_transfer.py`
- Tests: `scripts/production/test_staff.py`, `scripts/production/test_unit_names.py`, `scripts/production/test_add_unit_staff_row.py`, `scripts/agents/test_shutdown_cli.py`, `scripts/agents/test_producer_transfer.py`

**Workers:** 1 writer (`review4`).

**Order:** after phase 66, ahead of phases 60, 64 and 65. It ships alone on origin/main.

**Acceptance gate:**
1. Tests use fixture staff files only, never the live staff record.
2. Tests: each writer refuses `x-unit` with the one-line reason; an alias lookup of `x-unit` still finds row `x`; a record that already holds `x-unit` still loads. Each refusal test fails on main.
3. The staff, add_unit, shutdown and transfer suites pass. basedpyright: 0 errors, 0 warnings.
4. Change-Review: none, unless a visible result exists.

### Phase 68 — A producer can answer and withdraw its own question  · status: done
**Depends on:** none

#### Work Order

**Goal:** the sdlc showrunner (2026-11-18). dwight filed a question with the producer form (no `--production`, as `commands/showrunner/question.md` says). The user answered it in the dwight session at 08:45 EST. `update_registration.py answer` requires `--production`, so the producer could not record the answer, and the question still shows on the user's Questions page.

**Spec:**
1. `answer` and `withdraw` accept the producer form with no `--production`, as `ask` does. The producer's fixed staff id selects its question folder.
2. Add a row to the existing answer tests: a producer asks, then answers its own question with no `--production`, and the question leaves the open list. Do the same for withdraw.
3. Update `commands/showrunner/question.md` in the same commit.

**Files:**
- `scripts/production/update_registration.py`, `scripts/production/test_update_registration_questions.py`, `commands/showrunner/question.md`

**Workers:** 1 writer (`design`).

**Order:** after phases 60 and 65 in the queue; it shares no file with them.

**Acceptance gate:**
1. The new rows fail on main.
2. basedpyright: 0 errors, 0 warnings. The update_registration suites pass. Full gate green.

**Result:** `update_registration.py answer` and `withdraw` accept the producer form with no `--production`; the producer's fixed staff id selects its question folder. On main 3515c9470 (hash 3ce1c5a6d).

### Phase 69 — Park in /recall only hides an item from the user list  · status: done
**Depends on:** none

#### Work Order

**Goal:** the user (2026-11-18, via the sdlc showrunner): "recall is ONLY FOR MORTY - park is a MORTY specific directive to not show it to me on the list". Park never means a showrunner or unit director parks the work.

**Spec:** two edits to `commands/recall.md`, as the showrunner gave them: step 2 says park only hides the item until morty unparks it and never stops, delays or reorders work; the last line says `/recall` never changes the work, its owner or its timing.

**Files:** `commands/recall.md`

**Result:** `commands/recall.md` says park only hides the item until morty unparks it, and `/recall` never changes the work, its owner or its timing. On main 3515c9470 (hash 1d074db29).

### Phase 70 — A worker with an unknown run state does not break the census  · status: done
**Depends on:** none

#### Work Order

**Goal:** the sdlc showrunner (2026-11-18). The board showed the anomaly "census: workers[8].runs is not a list". Two faults sit behind it.

**Spec:**
1. Reader: `cargo-handler census --json` gives `runs: null` with `unknown.reason` for a worker. That is the handler's `Observed::unknown` form (cargo-handler `src/census/mod.rs` about lines 412 and 424). `scripts/production/census.py` about line 153 calls `_list(fields, "runs")` and rejects null. Accept the unknown form as an unknown run state for that worker, as the handler means it. Add a row to the existing census tests.
2. Writer: the handler's reason names a detached run record that holds only `{}`; its `.log` and `.scope` are gone, and its `.record.lock` stays. Find what wrote `{}` there, and fix that writer at the source. Read only in that run folder.

**Files:** `scripts/production/census.py`, its tests, and the writer that the cause names.

**Workers:** 1 writer.

**Order:** after phase 68.

**Acceptance gate:**
1. The new census row fails on main. The writer has a test that fails on main.
2. basedpyright: 0 errors, 0 warnings. Full gate green.

**Result:** a worker whose run state cannot be read shows as unknown, and the census goes on. `detached_runs.py` stops a run only after it verifies the process group again; the hold enforcer and the supervisor use the same check. On main f4663d63b (hash 609e4bdca).

### Phase 71 — Producer docs name the live accounts and models  · status: done
**Depends on:** none

#### Work Order

**Goal:** the sdlc showrunner (2026-11-18), from the producers plan. Docs match current reality.

**Spec:**
1. The producer skills in `commands/producer/` say Claude producers use the showrunners account. The live rule is the producers account, with the showrunners account as the fallback (`accounts.group_label`, `docs/accounts.md`). Correct the skills.
2. `docs/as-built/shutdown-and-restart.md` shows morty and brain on Codex. `config/models.conf` has all three producers on `claude:fable:high`. Correct the doc.

**Files:** `commands/producer/morty.md`, `commands/producer/brain.md`, `commands/producer/dwight.md`, `docs/as-built/shutdown-and-restart.md`

**Workers:** none; a doc fix by the director.

**Acceptance gate:**
1. A blind review finds no remaining mismatch. The full gate is green.

**Result:** the producer commands and the as-built docs say Claude producers use the producers account, with the showrunners account as the fallback, and show the live models from `config/models.conf`. On main 3515c9470 (hash 287f9256c).

### Phase 72 — One common producer command  · status: done
**Depends on:** phases 65 and 70 (queue order).

#### Work Order

**Goal:** the sdlc showrunner (2026-11-18), producers plan phase 2 (`~/rust/hanadocs/morty/producers-plan.md`, "Decisions from the review with nate" is final). The user named the command.

**Spec:**
1. Add `/producer:produce`. It holds the management text that each producer shares: the role, questions, investigations, transfer, messaging, and work with showrunners and morty.
2. Add `templates/producer/README.md` with the `{name}` and `{job}` fields only, and two sections: "Job" and "Current work". Agreed with devtools (2026-11-18): `agents add-producer NAME --job TEXT` reads this file, fills both fields and writes `~/rust/hanadocs/<name>/README.md`. It never overwrites a README. Send devtools any field change, and the hash when this phase lands.
3. Move morty, dwight and brain onto the common command. Remove the dwight and brain skills. The morty skill keeps only the List rules.
4. dwight helps with the text.

**Files:** `commands/producer/`, the template, and the start and resume code that names the producer skill.

**Workers:** 1 writer.

**Acceptance gate:**
1. dwight and brain start from the common command. Two skills are gone. brain has a folder and a README.
2. Full gate green.

**Result:** `commands/producer/produce.md` is the one producer command; each producer's job is in `<PRODUCER_FOLDER_ROOT>/<name>/README.md`. The brain and dwight skills are gone; brain has its README; dwight rewrites its own. On main 78de4e3ec (hash 3c7a8d4ad).

### Phase 73 — A change row can be withdrawn  · status: done
**Depends on:** phase 72 (queue order).

#### Work Order

**Goal:** the sdlc showrunner (2026-11-18), queue item rowwithdraw, from hana. hana/C386 (merge 8dcdeb04) was marked works, then reverted (efed8fe08b) and removed. The user agreed to record it as withdrawn.

**Spec:**
1. `change_review.py` gets one supported action: withdraw a row, open or reviewed. The row history keeps the user's words and the time. No hand edit of the list.
2. Removed (showrunner agreed 2026-11-18): the `list-ready` revert scan. Its skip shifted which row matched which checkpoint, it gave false and missed matches, and it cost 0.74 s of Git per scan. Withdraw covers a reverted change.
3. Add rows to the existing change_review tests. Update `commands/change_review.md` in the same commit.

**Files:** `scripts/production/change_review.py`, its tests, `commands/change_review.md`.

**Workers:** 1 writer.

**Acceptance gate:**
1. Each new test row fails on main. basedpyright: 0 errors, 0 warnings. Full gate green.

**Result:** `change_review.py` withdraws an open or reviewed row and keeps the user's words and the time in the row history. The `list-ready` revert scan is gone. On main 716ca5f73 (hash 03a78d761).

### Phase 74 — A unit's worktree repo selects its merge repo  · status: done
**Depends on:** none

#### Work Order

**Goal:** the sdlc showrunner (2026-11-18). `merge_checkpoint.py --production <p>` merges into the production's own repo. The style unit (staff parent cargo-liner) has a `~/.claude` commit 9d636482f. Under cargo-liner it merges into `~/rust/cargo-liner` and fails at ancestry. Under sdlc it is refused: "staff parent is cargo-liner; use that production".

**Spec:**
1. `--production P` names the destination. The merge uses P's checkout, branch, tests, push, promotion and lock, as on main. No repo-owner lookup: several productions merge into one repo (showrunner agreed 2026-11-18).
2. A unit whose staff parent is another production Q is accepted only when its worktree repo is P's repo. Q keeps the production log and the review ledger.
3. Overlap notices compare only units in P's repo. `merge.md` and `produce.md` say that an order runs under the production whose repo holds the commit, one batch for each repo.
4. Add rows to the existing merge_checkpoint tests: the style unit's `~/.claude` commit merges under sdlc and writes cargo-liner's log and ledger; its cargo-liner commit under sdlc is refused.

**Files:** `scripts/production/merge_checkpoint.py`, `checkpoint_gate.py`, their tests, `commands/showrunner/produce.md`, `commands/unit/merge.md`.

**Workers:** 1 writer.

**Order:** after phases 65 and 70, before other queue work.

**Acceptance gate:**
1. Each new test row fails on main. basedpyright: 0 errors, 0 warnings. Full gate green.

**Result:** `merge_checkpoint.py --production P` names the merge destination; P's checkout, branch, tests, push, promotion and lock apply. A unit whose staff parent is another production is accepted only when its worktree uses P's main checkout; the parent keeps its log and ledger. produce.md groups merge notices by destination production. On main 62aca78ee (hash c5980b520).

### Phase 75 — Review page follow-ups  · status: done
**Depends on:** none

#### Work Order

**Goal:** the sdlc showrunner (2026-11-18) made four add-ons current. This one closes phase 65 F005, F008 and F009 and the Done card defect (on main 3515c9470).

**Spec:**
1. F005: a reload during a snapshot publication retry keeps the browser save marker until the snapshot confirms the verdict. The server does not return the old verdict form (review_page.py ~391, ~826, ~985).
2. F008: a reload after a Show change restores the saved browser state. The jump applies only to a new link (review_page.py ~1208).
3. F009: a link restored by Back during a save applies after the save ends, for Changes and Questions. The save does not clear the other card or its link (review_page.py ~1212, ~1067; record_table.py ~388).
4. Done card: the Proof heading shows only when proof text exists (~562). A Done card has no Later button (~516).
5. Add rows to the existing review_page and record_table tests.

**Files:** `scripts/review_links/review_page.py`, `scripts/review_links/record_table.py`, their tests.

**Workers:** 1 writer.

**Order:** merge unit-board's reviewfade tip before the hash. It also changes the Review page.

**Acceptance gate:**
1. Each new test row fails on main. basedpyright: 0 errors, 0 warnings. Full gate green.

**Result:** A reload during a snapshot publication retry keeps the browser save marker until the snapshot confirms the verdict. A reload after a Show change restores the saved browser state, and the jump applies only to a new link. A link restored by Back during a save applies after the save ends and after every row animation finishes, without clearing the other card. A Done card shows no Later button, no Your words box and no empty Proof heading. Merged with reviewfade and reviewask. On main 558420376 (hash 299999db9).

### Phase 76 — Detached run hardening  · status: done
**Depends on:** phase 70

#### Work Order

**Goal:** the sdlc showrunner (2026-11-18). Close phase 70 F016-F019, four rare races. Use the smallest fix for each.

**Spec:**
1. A stop request stays pending until the signal is delivered. A failed rollback after a failed sync leaves no stop reason (detached_runs.py ~245).
2. The shared record reader checks the run id against the file name. Reap never queues a completion for the wrong run (~223).
3. Reap does not keep a dead run pending when another group reuses its id (~354).
4. The process check tells an absent process from a failed ps call. A failed check stays pending (detached_run_process.py ~32).
5. Add rows to the existing detached_runs tests.

**Files:** `scripts/agents/detached_runs.py`, `scripts/agents/detached_run_process.py`, `scripts/agents/test_detached_runs.py`.

**Workers:** 1 writer.

**Order:** on the phase 70 tip. Merge unit-board's panestop and panedone2 tips before the hash; they change detached_runs.py.

**Acceptance gate:**
1. Each new test row fails on the phase 70 tip. basedpyright: 0 errors, 0 warnings. Full gate green.

**Result:** A stop request stays pending until its signal is delivered, through a `.stop-staged` file that a failed rollback cannot leave behind. The shared record reader checks the run id against the file name, reap never queues a completion for the wrong run or keeps a reused id pending, and the process check tells an absent process from a failed ps call. Merged with panestop and panedone2: staged pane stops survive the supervisor retry, and the reaper completes each pending record once. F009 (a crashed pane stopper plus a concurrent hold stop) moved to a follow-up. On main 51d3608ad (hash a2c3d583d).

### Phase 77 — Change review hardening  · status: done
**Depends on:** phase 73

#### Work Order

**Goal:** the sdlc showrunner (2026-11-18). Close phase 73 F010 and F011.

**Spec:**
1. The strict delivery read rejects a pending verdict slot with no attempt id (change_routing.py:338, delivery_state.py:123).
2. `end_row_scratch` reports a `conf.Unusable` scratch config and lets a retry finish the cleanup, for every caller (change_review.py:1978).
3. Add rows to the existing change_routing, delivery_state and change_review tests.

**Files:** `scripts/production/change_routing.py`, `scripts/production/delivery_state.py`, `scripts/production/change_review.py`, their tests.

**Workers:** 1 writer.

**Order:** on the phase 73 tip.

**Acceptance gate:**
1. Each new test row fails on the phase 73 tip. basedpyright: 0 errors, 0 warnings. Full gate green.

**Result:** The strict delivery read rejects a pending verdict slot with no attempt id. `end_row_scratch` reports an unusable scratch config and a retry finishes the cleanup. Every browser verdict path keeps a cleanup record, which loads only when private and owned by the current user. Three rare races moved to a follow-up item (F003, F004, F005). On main ebbb81613 (hash 3cfa74c4f).

### Phase 78 — Cross-repo units  · status: done
**Depends on:** phase 74

#### Work Order

**Goal:** the sdlc showrunner (2026-11-18). Close phase 74 F010 and F011.

**Spec:**
1. The unit board reads dependency evidence from the unit's own repo. It labels a failure by the land destination (unit_phases.py:1454).
2. A batch review report returns each row with its own list path (merge_checkpoint.py:549).
3. Add rows to the existing unit_phases and merge_checkpoint tests.

**Files:** `scripts/production/unit_phases.py`, `scripts/production/merge_checkpoint.py`, their tests.

**Workers:** 1 writer.

**Order:** on the phase 74 tip.

**Acceptance gate:**
1. Each new test row fails on the phase 74 tip. basedpyright: 0 errors, 0 warnings. Full gate green.

**Result:** The unit board reads merge and dependency evidence from each unit's own repo through one landed-evidence rule, and labels a failure by its land destination. An incomplete run history shows "merge evidence unavailable". A legacy land result with no production key no longer crashes the board. A batch review report gives each row its own list path. A same-repo board keeps four Git calls. Multi-run cases (F006, F008, F010, F012, F013) moved to one cross-repo board follow-up after five review rounds. On main f6c11ee64 (hash b5ab8a1e3).

### Phase 79 — Review-trial rows keyed by checkpoint  · status: done
**Depends on:** none

#### Work Order

**Goal:** the sdlc showrunner (2026-11-18). `review_regime.py add` keys a row on unit, plan and phase only. natemerge batch postmaster2 (main dc2481efc) printed "kept postmaster phase 1 of permanent: a row exists from the merge at 2026-11-18T12:35:22-05:00; nothing added". Those were two different postmaster phases (the relay prefix at 12:35, then the no-lead-in change 45bef9a09). Both were "phase 1" of plan "permanent", so the second row was lost.

**Spec:**
1. A row records its checkpoint commit. `merge_checkpoint.py` passes `--checkpoint <commit>` to `review_regime.py add`.
2. Two rows name the same phase only when unit, plan, phase and checkpoint match. A rerun of the same merge still adds nothing. A row written before this change has no checkpoint; it matches only a new row with the same unit, plan, phase and started time.
3. Record the lost row once: postmaster phase 1 of plan permanent, checkpoint 45bef9a09, started 2026-11-18T13:05:00-05:00, merged at its merge commit time on main, review trial "ux 0 findings, code 0 findings, review-worker minutes 4, ux check minutes 0, ux repair minutes 0", excluded "no merge design check". Source: /tmp/claude/unit/d74163b1-8ec5-4722-9b1d-4ab50215c9a5/phases/postmaster2/batch.txt. The director runs the one `review_regime.py add` on natedev after the phase lands.
4. Add rows to the existing review_regime and merge_checkpoint tests.

**Files:** `scripts/production/review_regime.py`, `scripts/production/merge_checkpoint.py`, their tests.

**Workers:** 1 writer.

**Order:** after phases 75 to 78.

**Acceptance gate:**
1. Each new test row fails on main. basedpyright: 0 errors, 0 warnings. Full gate green.

**Result:** A review-trial row records its full checkpoint hash, and `merge_checkpoint.py` passes `--checkpoint`. Two rows name one phase only when unit, plan, phase and checkpoint match; a row with no checkpoint matches by start time. `--checkpoint` stays optional for the landing merge; a follow-up makes it required. The lost postmaster 45bef9a09 and admin-console reviewask 6aff8187c rows are in the natedev ledger. On main b8e30ad51 (hash 767ac1df9).

### Phase 80 — Review-trial rows require a checkpoint  · status: done
**Depends on:** phase 79

#### Work Order

**Goal:** the sdlc showrunner (2026-11-18), the phase 79 follow-up. Phase 79 kept `--checkpoint` optional and a start-time match for rows with no checkpoint, only for the landing merge. Main now passes `--checkpoint` on every merge, so both go. No old-shape reader stays in main code (user rule 11-05).

**Spec:**
1. `review_regime.py add` requires `--checkpoint`. A call without it fails with a usage error and writes nothing.
2. Two rows name the same phase only when unit, plan, phase and checkpoint match. Remove the start-time match.
3. A ledger row written before phase 79 has no checkpoint, and none is known for it. It stays as a recorded history row: reports and watch read it, and no new row ever matches it.
4. Update the as-built docs that describe the optional flag and the start-time match.
5. Change rows in the existing review_regime and merge_checkpoint tests; add no new module.

**Files:** `scripts/production/review_regime.py`, `scripts/production/merge_checkpoint.py`, their tests, `docs/as-built/review-regime-trial.md`, `docs/as-built/showrunner-automation.md`.

**Workers:** 1 writer.

**Order:** on main 51d3608ad. No other open phase touches these files.

**Acceptance gate:**
1. A test fails on main for a missing `--checkpoint` and for a start-time match. basedpyright: 0 errors, 0 warnings. Full gate green.

**Result:** `review_regime.py add` requires `--checkpoint`, and two rows match only when unit, plan, phase and checkpoint match; the start-time match is gone. Older ledger rows with no checkpoint stay as history rows that report and watch still read, and no new row matches them. On main 49b3cd5da (hash a31f4cbff).

### Phase 81 — A new registry row goes after its section's last key  · status: done
**Depends on:** none

#### Work Order

**Goal:** the sdlc showrunner (2026-11-18), from producer bob's create. `registry_rows.replay` adds a new row when it reaches the next section header. So `bob=claude:fable:high` landed in `config/models.conf` after the comment block of the production section, just before `[production.claude]`. The parser still reads it in `[producers]`.

**Spec:**
1. A new row goes directly after the last key line of its section (for `[producers]`, after `dwight=`). Comments and blank lines that follow the last key stay below the new row. A section with no keys gets the row directly after its header.
2. Move bob's line in `config/models.conf` to after `dwight=` in the same change. The value stays the same, so this is not a value change.
3. Add rows to the existing registry tests (`scripts/lib/test_models_registry.py` or `scripts/agents/test_models_conf_publish.py`); add no new module.

**Files:** `scripts/lib/registry_rows.py`, its existing test, `config/models.conf`.

**Workers:** 1 writer.

**Order:** on main 83f0a961e. No other open phase touches these files.

**Acceptance gate:**
1. The new test row fails on main. basedpyright: 0 errors, 0 warnings. Full gate green.

**Result:** `registry_rows.replay` inserts a new row directly after the last key of its section, and a new section at the end of the file. Comments and blank lines after the last key stay below the new row. `bob=` in `config/models.conf` now follows `dwight=`. On main bec23322d (hash 96accc3fa).

### Phase 82 — A producer sets the state and check time of its own List items  · status: done
**Depends on:** none

#### Work Order

**Goal:** the user (19:58 EST, via morty and the sdlc showrunner, 2026-11-18): "sure absolutely let a producer write the state and check time of its own items". Today a producer can update only `--latest-status`, `--next-step` and `--eta` (6c7fc5687).

**Spec:**
1. `the_list.py update` from a producer also accepts `--state` and `--next-check` on an item where it is the producer.
2. Limits, set by morty, the List page producer: own items only. Owner, producer, title, done-when, related-to, and the add, close, park and link commands stay with morty. A producer cannot set Closed. Each write records its source, as now.
3. The producer assignment notice and the refusal text name the five options.
4. Update `commands/producer/produce.md` and `docs/as-built/showrunner-automation.md` where they list the producer options.
5. Add rows to the existing producer tests in `test_the_list.py`: an allowed state, a refused Closed, a refused item of another producer, a written next check, and the recorded source.

**Files:** `scripts/production/the_list.py`, `scripts/production/test_the_list.py`, `commands/producer/produce.md`, `docs/as-built/showrunner-automation.md`.

**Workers:** 1 writer.

**Order:** after phase 81. Unit-board's parkdelivered and listfollow also change the_list.py; merge main after they land, before the hash.

**Acceptance gate:**
1. Each new test row fails on main. basedpyright: 0 errors, 0 warnings. Full gate green.

**Result:** a producer sets `--state` (not Closed) and `--next-check` on its own List items, and the List records the source. A producer cannot change another producer's item. Landed inside the phase 83 merge, on main 69b0e7ae6 (hash 02977b702).

### Phase 83 — Tools add the present time; agents write only future times  · status: done
**Depends on:** phase 82

#### Work Order

**Goal:** the user (Questions page Q510, 22:40 EST, 2026-11-18, via the sdlc showrunner): "answer: Tools add the time: sdlc changes the two tools, and the rule for agents becomes: write no present time." Name: toolclock. morty gave the producer-file text and two conditions (via the sdlc showrunner).

**Spec:**
1. `the_list.py update` records the clock time of each update. Reuse the existing time in the record (`updated` and the log entry time); add no second field.
2. The List page shows that time beside the latest status as `HH:MM <zone label>` in the user zone from `user_zone.py`. Give the full date when the update is not from today.
3. `--source` gets its time from the tool. Producers now write `<who: words>, HH:MM EST` by hand. The tool shows the source with `, HH:MM <zone label>` from the log entry time. A `--source` that ends in a clock time is refused, with text that says the tool adds the time.
4. `send.py` adds one header line beside "Shown sender", for example `Sent: 22:41 EST`, in the user zone. Do this part only after unit-board's workermsg is on main (it also changes send.py). Merge main first.
5. SendMessage between Claude sessions gets no change. The rule covers it.
6. Change the old rule ("read the clock, then write the time") to: "Write no present time. Write only future times, such as an ETA. The tools add the present time." Places: the `zone.py` notice text, `commands/showrunner/produce.md` (Throughout, Time), and `commands/unit/announce.md`.
7. `commands/producer/produce.md`, "Messages and reports", morty's text: replace "Include the time zone with each time. Send documents as review links." with "Write no present time; the tools add it. Write each past or future time, such as a start or an ETA, with its time zone. Send documents as review links." Where produce.md tells producers to use `--source`, say that they write no time there. morty's condition: this text ships in the same commit set as the tool change, not before.
8. Add rows to the existing tests (`test_the_list.py`, `test_list_page.py`, `test_zone.py`, the send.py header test); add no new module.

**Files:** `scripts/production/the_list.py`, `scripts/review_links/list_page.py`, `scripts/message/send.py`, `scripts/lib/zone.py`, their existing tests, `commands/showrunner/produce.md`, `commands/unit/announce.md`, `commands/producer/produce.md`.

**Workers:** 1 writer.

**Order:** stacked on phase 82 (both change `the_list.py update`); it does not wait for phase 82 to merge. The send.py part waits for unit-board's workermsg on main.

**Acceptance gate:**
1. Each new test row fails on main. basedpyright: 0 errors, 0 warnings. Full gate green.

**Result:** `the_list.py update` keeps the clock time of each update, and the List page shows it beside the latest status in the user zone. The tool adds the time to `--source` and refuses a source that ends in a clock time. `send.py` adds a `Sent:` header line. The agent rule is now "write no present time; write only future times". On main 69b0e7ae6 (hash d6d56f52f).

### Phase 84 — The List refuses an ETA that is not one of the agreed forms  · status: done
**Depends on:** phase 83

#### Work Order

**Goal:** nate (00:49 EST Nov 19, List 106, via morty and the sdlc showrunner). An ETA is the time when the whole item is done. The producer docs that name the forms ship with phase 83 (336f042c4).

**Spec:**
1. `the_list.py add` and `update` accept `--eta` only in these forms:
   - `Mon D` or `Mon D HH:MM ZONE` (for example `Nov 19` or `Nov 19 14:30 EST`). No "about", no "estimate", no text after it. ZONE is the user zone's label from `user_zone.py`. The year is the one that puts the date nearest to today.
   - `No time yet`, `Waiting for nate`, `Paused`, `Delivered`.
2. Any other `--eta` is refused. The error names the forms and says: put the reason in --latest-status.
3. Old records stay as they are, with no conversion. The reader accepts every stored form it reads now (ranges, ISO times, text, `Not asked`, `Pending`).
4. The List page gets no change from this phase. unit-board owns the page ETA column.
5. A producer may write `--evidence` on its own items, under the same check as `--latest-status`, `--next-step`, `--eta`, `--state` and `--next-check`. Other fields stay morty's (nate via morty, List 106).
6. `--add-evidence "<text>"` adds one line to the end of the evidence text. `--evidence` still replaces the whole text. The same producer check covers both.
7. `commands/producer/produce.md`: add `--evidence` and `--add-evidence` to the option line "Use `--latest-status`, `--next-step`, `--eta`, `--state`, or `--next-check`." This line ships with the tool, so the doc matches the tool.
8. Add rows to the existing tests in `test_the_list.py`: each accepted form, a refused `about Nov 19`, a refused range, a refused free text, an old record that still reads, a producer `--evidence` and `--add-evidence` on its own item, and a refused one on another producer's item.

**Files:** `scripts/production/the_list.py`, `scripts/production/test_the_list.py`, `commands/producer/produce.md`, `docs/as-built/showrunner-automation.md` (where it lists the ETA forms and the producer options).

**Workers:** 1 writer.

**Order:** stacked on phase 83 (both change `the_list.py update`).

**Acceptance gate:**
1. Each new test row fails on main. basedpyright: 0 errors, 0 warnings. Full gate green.

**Result:** `the_list.py add` and `update` accept `--eta` only as `Mon D`, `Mon D HH:MM ZONE`, `No time yet`, `Waiting for nate`, `Paused` or `Delivered`. Old records read as before. A producer may set `--evidence` and `--add-evidence` on its own items. On main 31f454605 (hash fb52e3dc7).

### Phase 85 — send.py prints FAILED only when delivery failed  · status: done
**Depends on:** phase 84 (fb52e3dc7)

#### Work Order

**Goal:** the sdlc showrunner (06:30 EST Nov 19; phases-unit owns send.py). verify-unit sent two messages to builds at 06:27 and 06:28. Copy 1 printed `FAILED: SendMessage used a different target or payload`. Copy 2 printed `FAILED: 2 SendMessage calls; delivery unconfirmed`. The resend rule then made copies, and the repeat guard held copy 3 as a flood.

**Spec:**
1. The blind review traced both sends. The 06:27 copy was not delivered: no proxy write, one receipt in builds. Its FAILED was correct. The 06:28 copy made two SendMessage calls; the proxy refused the first and wrote the second, which returned success. Only that FAILED was false.
2. `read_relay` (send.py) selects the SendMessage call by the proxy forward's tool ID, not by a count of one. The other calls become a note on the SENT line.
3. SENT still needs a completed proxy forward of the approved text and one success result for the forwarded tool ID. A completed `sendall()` alone does not prove the recipient read the message.
4. A refusal result, a missing result, a timeout or a socket error after the write keep their old results (not SENT).
5. Add rows to the existing relay tests in `test_send.py`: the 06:28 shape prints SENT; a refusal after the write and a timeout after the write stay not SENT; the no-forward and interrupted rows stay as controls.

**Files:** `scripts/message/send.py`, `scripts/message/test_send.py`, `docs/as-built/` where it states the relay result rules.

**Workers:** 1 writer.

**Order:** branch from the phase 84 hash fb52e3dc7 (sdlc showrunner, 06:35 EST Nov 19). The showrunner orders it after the console batch.

**Acceptance gate:**
1. Each new test row fails on main. basedpyright: 0 errors, 0 warnings. Full gate green.

**Result:** `send.py` picks the SendMessage call by the proxy forward's tool ID. The SENT line notes the other calls. A refusal, a missing result, a timeout or a socket error after the write stay not SENT. On main 1a3e942f1 (hash 75e6bfa6b).

### Phase 86 — A merge run survives an account move, and a changed batch after a stopped run is refused  · status: done
**Depends on:** none (branch from main 815070e57)

#### Work Order

**Goal:** the sdlc showrunner (about 10:00 EST Nov 19; nate approved both on the Review page at 09:48 EST, via dwight). On Nov 19 the 06:55 and 07:16 EST account moves killed spacetime merge batches 1119a and 1119c, one during its test. At about 01:00 EST spacetime reran stopped batch 1118c with one more row; `resume_merged_items` resumed the merged items alone and tested an item without the new fix.

**Spec:**
1. A `merge_checkpoint.py` run outlives an account move of the session that started it. Find the real cause of the 1119a and 1119c stops first: the process tree, the starting route (detached_runs.py or a direct call) and what the move ended.
2. Start the run in its own process session, away from the starting session. The move must not end it.
3. Keep the run record, so a stop still goes through `detached_runs.py stop`. Add no signal handler.
4. When a stopped batch left unpushed merges and the new batch differs from it, `merge_checkpoint.py --batch` refuses. The refusal names the step: reset the merge branch to its pushed tip, then start a fresh batch.
5. A rerun of the same stopped batch keeps today's resume.
6. Add rows to the existing tests: a move-shaped stop of the starting session leaves the run live; a changed batch over unpushed merges refuses with the step; the same batch resumes.

**Cause (impl, 10:05 EST):** the showrunner fallback in `commands/showrunner/produce.md` ran merge_checkpoint.py as a background Bash task. The account move stops the Claude process, and its task cleanup stopped the run.

**Design:** `detached_runs.py start` accepts a showrunner caller, with the unit director route (run folder, own process session, record, result through send.py). The produce.md fallback starts merge_checkpoint.py through `detached_runs.py start`.

**Files:** `scripts/production/merge_checkpoint.py`, its tests, `scripts/agents/detached_runs.py`, `test_detached_runs.py`, `commands/showrunner/produce.md`, `docs/as-built/` where it states the merge run and detached caller rules.

**Workers:** 1 writer.

**Order:** after phase 85; the showrunner orders the merge.

**Acceptance gate:**
1. Each new test row fails on main. basedpyright: 0 errors, 0 warnings. Full gate green.

**Result:** the showrunner fallback starts `merge_checkpoint.py` through `detached_runs.py start`, in its own process session, so an account move no longer stops it. A `--batch` run that differs from a stopped batch with unpushed merges is refused, and the refusal names the reset step. A rerun of the same batch still resumes. On main 10fd385ee (merged as 5aff530c8, hash 6214af197).

### Phase 87 — A busy staff row names the command and PID that hold it  · status: done
**Depends on:** none

#### Work Order

**Goal:** sdlc, from natedev's accounts analysis of the 07:16 EST move failure (2026-11-19). Name: slotholder. Today the error "another operation holds this row" does not name the holder, so nobody can tell which process blocks a move.

**Spec:**
1. `scripts/production/staff.py`, `slot_operation`: after it takes the lock, write the holder's PID and command into the lock file. Replace the old content. A joined child operation writes nothing.
2. When the lock is busy, the error names the holder: `<name>: another operation holds this row (pid <pid>: <command>)`. If the file has no holder yet, keep the old text.
3. Keep one place that writes and one place that reads the holder line.
4. Add a row to an existing test in `test_staff_operation.py`; add no new test module.

**Files:** `scripts/production/staff.py`, `scripts/production/test_staff_operation.py`, `docs/as-built/` where it states the row lock error.

**Workers:** 1 writer.

**Order:** while phase 86 waits for its gate. Tests run only when natemerge allows them.

**Acceptance gate:**
1. The new test row fails on main. basedpyright: 0 errors, 0 warnings. Full gate green.

**Result:** `slot_operation` writes the holder's PID and command into the lock file. A busy row error names the holder: `<name>: another operation holds this row (pid <pid>: <command>)`. On main 10fd385ee (merged as 6b648d50b, hash c2edd1f3e).

### Phase 88 — Merges run no type check; the nightly type check sends each red to its author  · status: done
**Depends on:** none

#### Work Order

**Goal:** the user (Review page, 15:34 EST Nov 19, via sdlc): "i want no type checks done on merges - i want type checks done in the nightly type check and handed out to whomever changed the code that broke it - to fix it". Name: notypemerge.

**Spec:**
1. Remove every type check from the merge paths:
   - the `basedpyright` command in the Merge tests field of `docs/plans/sdlc-production.md` and `docs/plans/natedev-production.md`;
   - the `basedpyright` step in `fast_track.py`;
   - the `--type-dirs` option of `merge_gate.py`. The nightly run keeps its type check through `NIGHTLY_TYPE_DIRS`.
2. `docs/production_format.md` and `commands/producer/to_production.md` say that Merge tests name no type check.
3. The nightly type check finds the author of each type red. It blames the diagnostic line and maps the commit to the unit whose merge brought it in.
4. The nightly run sends each author unit its own diagnostics and records the fix as assigned. A diagnostic with no author keeps today's path: sdlc assigns it.
5. Update the as-built docs in the same commit.
6. Add rows to existing tests; add no new test module.

**Files:** `scripts/production/merge_gate.py`, `scripts/production/fast_track.py`, their existing tests, the two production docs, `docs/production_format.md`, `commands/producer/to_production.md`, `docs/as-built/`.

**Workers:** 1 writer.

**Order:** before the item 121 work (sdlc). Tests run only when natemerge allows them. builds removes the `--type-dirs` readers in `first_step.py` and `memory_admit.py` after this merges.

**Acceptance gate:**
1. No merge path runs basedpyright. The nightly run still type checks `scripts`. basedpyright: 0 errors, 0 warnings. Full gate green.

**Result:** no merge path runs basedpyright. The nightly type check blames each diagnostic line, finds the unit that merged it, and sends that unit its own diagnostics. A diagnostic with no author goes to sdlc. On main f7a28f40d (merged as 905c9d003, hash 9ac0328f7).

### Phase 89 — A session in a review gets its messages at the review end; an urgent message goes through at once  · status: done
**Depends on:** none

#### Work Order

**Goal:** the user approved this at 17:27 EST Nov 19 in the dwight session: "okay build it". His one requirement: a simple way for a sender to send an urgent message through the hold. Name: reviewhold. During an adhoc review, each peer report starts a turn that is not about the item.

**Spec:**
1. While a session has an open review (its `adhoc_items.py` count is above 0), `send.py` holds each message to it. It does not deliver the message.
2. When the review ends (`adhoc_items.py 0`), the session gets all held messages as one list, oldest first. Each entry shows its sender and Sent time.
3. `send.py --urgent` delivers at once through the hold. It is for a point that needs nate before a merge, a loss of data, or a stall. `commands/message.md` names the flag.
4. The send result says the message is held, for example `HELD: <target> is in a review; delivered at its end`. A held message counts as kept, so a sender never resends it.
5. Timer prompts that go through `send.py` (stall-watch, update timers) are held the same way. In-process subagent reports are harness task notices; the hold cannot reach them.
6. A crash or a missed end loses no held message. The next `adhoc_items.py 0` delivers them, and so does session start.
7. The hold also ends when the session had no user prompt for N minutes (sdlc, 17:29 EST). N is one new `send.conf` key, default 30. At that end the held list is delivered as at count 0. A later user prompt does not start the hold again; only a new review does (the count rises from 0).
8. No signal handlers or traps. Tests write their own fixtures and run on no live unit. Add rows to existing tests.

**Files:** `scripts/message/send.py`, `config/send.conf`, `scripts/statusline/adhoc_items.py`, one session-start hook, the user-prompt hook if the idle end needs it, `commands/message.md`, `commands/adhoc_review.md`, the producer skills that name message rules, their existing tests.

**Workers:** 1 writer.

**Order:** the message-rule text goes to sdlc and morty before the hash; the hash waits for morty. Tests run only when natemerge allows them.

**Acceptance gate:**
1. A fixture review session gets no delivery while its count is above 0, gets one oldest-first list at 0, and gets an urgent message at once. basedpyright: 0 errors, 0 warnings. Full gate green.

**Result:** while a session has an open review, `send.py` holds each message to it and delivers the held list at the review end. `--urgent` goes through the hold at once. A held send reports KEPT. A SessionStart hook delivers messages held for a session when it starts. On main 138c42247 (merged as 088fe8f12, hash ee9927b72).

### Phase 90 — A merge takes a moved main with no retest when the new main commits change only disjoint non-code files  · status: dropped
**Result (2026-11-19):** dropped with no merge. bob stopped it at 18:18 EST for the user's side (item 140 (a)). The case that started it, a settings.json commit on main, still retests under the rule. The rule passes only 29 of 554 main moves in 7 days. Review also found that the "non-code" files hold test inputs. The branch `sdlc-phases-90` stays off the merge line. A direct push to main still asks natemerge for a gap first.

### Phase 91 — A direct config or settings push to main waits for a gap in the land line  · status: todo
**Depends on:** none

#### Work Order

**Goal:** a decision by bob and builds, sent by sdlc at 18:19 EST Nov 19. Name: gappush. It replaces phase 90. Each command that pushes a config or settings commit direct to main takes its place in natemerge's land line. It pushes only in a gap between two items. Until this merges, a direct main push asks natemerge for a gap first.

**Spec:**
1. Put the change in the shared publish path from phase 40 (`live_settings.publish`, the main push lock, one gap function in `land_queue.py`), not in each caller. It covers the tool publishers only: tune, zone, the notes timer and registry rows. Hand commits (`/validate_and_push --to main` of a settings.json commit, the case a296444e4, and fast track of a `.conf`) and the CI receipt after a queued push are not in this phase (sdlc, 04:08 EST Nov 20). They go to phase 92. Until then a hand push during a land asks natemerge for a gap first.
2. The live value still changes at once on natedev and on the Mac. Only the main commit and the push wait for the gap.
3. When no land runs, the push goes at once. When a land runs, the publisher waits for a gap in its own detached process (sdlc, 04:46 EST Nov 20). It uses no wake files, no row in the land line and no runner-side replay; `next` keeps its base behavior. One gap lock: the waiter and land registration both take it, and the waiter holds it until its push process exits. The prepared commit is pushed before local main advances.
4. A push that waits shows where it waits and does not block the caller's turn. One waiter covers all waiting changes. It cleans up after itself.
5. Add no new check or refusal beyond the wait. Ask sdlc first if one seems needed.
6. A promote into the live checkout with a waiting `.conf` value does not fail and keeps the value: when the next group changes the same file, and when it does not (builds, 18:23). settings.json keeps its base route: phase 91 adds no settings.json publisher, journal or move recovery (sdlc, 06:41 EST Nov 20). The settings.json promote case moves to phase 92.
7. Tests use fixture repos and line state only, and one test proves that no write reaches the live land line, also when the live file is absent (sdlc, 03:15 EST Nov 20): push at once, wait in a detached process, a second save starts no second waiter, a land that starts during the push waits for it, and the promote cases for a `.conf` value.

**Files:** `scripts/lib/live_settings.py`, `scripts/lib/main_push_lock.py`, `scripts/lib/models_registry.py`, `scripts/lib/waiting_rows.py`, `scripts/production/land_queue.py`, the promote in `merge_checkpoint.py` only if a test needs it, their tests, the docs that tell a session how to change settings.json, `docs/as-built/merge-gate-and-test-speed.md` (Land line).

**Workers:** 1 writer.

**Order:** after phase 89. Tests run only when natemerge allows them.

**Acceptance gate:**
1. The spec cases pass. basedpyright: 0 errors, 0 warnings. Full gate green.

### Phase 92 — A hand push of a settings commit to main waits for a gap in its own process  · status: todo
**Depends on:** Phase 91

#### Work Order

**Goal:** sdlc, 04:08 EST Nov 20. Phase 91 covers the tool publishers only. A hand push of a settings commit (`/validate_and_push --to main` of settings.json, the case a296444e4, and fast track of a `.conf`) can still break a running land.

**Spec:**
1. `/validate_and_push --to main` and fast track change no commit. They ask natemerge's land line for a gap and wait in their own detached process, outside the caller's turn.
2. The caller's reply says the push waits for a gap and where. The push goes in the gap and cleans up after itself.
3. Add no new check or refusal beyond the wait.
4. CI starts after the push goes in, from the pushed commit.
5. A promote into the live checkout with a waiting settings.json value does not fail and keeps the value: when the next group changes settings.json, and when it does not (moved from phase 91, sdlc 06:41 EST Nov 20).
6. Tests use fixture repos, line state and slots only. One test proves no write reaches the live land line or `/build/slot`.

**Files:** `scripts/validate_and_push/push_direct.sh`, `scripts/production/fast_track.py`, `scripts/production/ci_points.py`, `scripts/production/land_queue.py` (the phase 91 gap function), their tests, `docs/as-built/validate-and-push-cancel-prior.md`.

**Workers:** 1 writer.

**Acceptance gate:**
1. The spec cases pass. basedpyright: 0 errors, 0 warnings. Full gate green.

### Phase 93 — A merge batch fetch survives a branch rename on origin  · status: todo
**Depends on:** Phase 86 (on main 10fd385ee)

#### Work Order

**Goal:** sdlc add-on after phase 86 (Nov 19), with the phase 86 minors F024-F026. Name: renamefetch. A branch rename on origin (`topic` to `topic/rework`) with a cached `origin/topic` breaks the `--no-prune` wildcard fetch in `merge_checkpoint.py` (`on_origin` and the batch fetch): git cannot write `refs/remotes/origin/topic/rework` while the file `refs/remotes/origin/topic` exists.

**Spec:**
1. Fetch each named ref to an explicit destination, not through the wildcard `+refs/heads/*:refs/remotes/origin/*`. A rename elsewhere on origin cannot then break the fetch.
2. Delete no tracking ref that other worktrees share. Add no `--prune`.
3. F024: a ref item whose tracking ref a narrow fetch config leaves stale gets the fresh tip.
4. F025: a cached phase-branch ref item plus an unrelated rename on origin still fetches.
5. F026: a merge-branch rename between a stopped batch and its rerun does not hide the stopped merge from the guard. Identify a checkpoint merge apart from the branch name.
6. Add rows to the existing `merge_checkpoint` tests; add no new test module. Tests use fixture repos only, never live state.

**Files:** `scripts/production/merge_checkpoint.py`, its existing tests, `docs/as-built/merge-gate-and-test-speed.md` where it states the batch fetch.

**Workers:** 1 writer.

**Order:** branch from main 10fd385ee. Tests run only when natemerge allows them.

**Acceptance gate:**
1. Each new test row fails on main. basedpyright: 0 errors, 0 warnings. Full gate green.

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

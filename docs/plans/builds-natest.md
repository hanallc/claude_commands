# natest

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** A standing plan with no end: lower the AWS CPU that `~/.claude` test gates use each day.

## Source

2026-11-03 09:30 EST

From the user (via natedev): Mission: find the longest-running ~/.claude tests and make them faster. Keep a descending sort of test times; when it makes the longest one shorter, it takes the next one. Build tools that instantly show the history of ~/.claude test suite times and of individual test times. Use parallelism, removal of trivial tests, shared code and good engineering practice. It must also send architectural change proposals back. It organizes its work in phases so /unit:direct works, and keeps its own phase backlog: the user wants the standard process engaged.

2026-11-06 12:15 EST

From the user (via the showrunner): the verify is a good improvement. i think that it needs to recalibrate its approach to test improvement based on this feedback

From the user (via the showrunner, 12:19 EST): natest should be the one doing the test work, of course

The feedback: verify's Phase 41 cut `test_verify_mac_arguments.py` from 56.0 to 30.2 CPU s. It tests logic by direct calls and keeps a few real runs.

2026-11-20

From the user: "i approve all - and i want you to work this into a plan for natest - a plan that is recurring and unending"
From the user: "keep your eyes on the prize - reducing the cost of our aws runs by reducing the cpu utilization of our ~/.claude repo tests"

## Standing work

This plan has no end. An agent with no memory runs one cycle from this section alone.
`show.py` is `~/.claude/scripts/lib/py ~/.claude/scripts/test_times/show.py`. The "next file" is `docs/plans/builds-natest-next.md`.

### The prize

- The prize is one number: the AWS CPU that `~/.claude` test gates use each day. It is the mean CPU of a green full gate, times the gates each day. (user, 2026-11-20)
- In money the prize is small: all AWS merge gate runs cost about 87 USD each month, and box start and setup is 1.1 min of each 7.3 min run (account cost and launch log, 11-13 to 11-19). The CPU work buys gate time and capacity.
- Test count is a cause to treat. It is never a score, a report or a gate. (user, 2026-11-20)
- Keep these three header numbers. Update them by hand at each refill, with host and date.
  1. AWS full gate, green, mean CPU: 4,192 s on 11-16 (low), 5,602 s on 11-19 (last AWS day). The first target is to return to the 11-16 value.
  2. Full gates each day: about 150 on all hosts (computed: 1,933 gates from 11-08 to 11-20). AWS ran 1,674 of them and used 82% of the gate CPU.
  3. Top 10 modules, AWS median CPU s, 11-19: `copies/test_slot_order.py` 156.9; `statusline/test_statusline.py` 154.2; `review_links/test_list_page.py` 114.1; `direct/test_verify_doc_checkout.py` 59.6; `direct/test_verify_wait_holder.py` 55.3; `production/test_fast_track.py` 50.3; `style/test_crate_run.py` 49.3; `agents/test_models_conf_publish.py` 47.4; `direct/test_verify_mac_only.py` 46.9; `direct/test_verify_ssh_targets_routing.py` 42.7. They are 13.8% of the module sum (5,632 s in 867 modules).

### Measure rule (plan-set, 2026-11-20)

Measure on AWS when AWS has a full gate in the last 2 days. If not, measure on natedev (`show.py top`, default host) and label each number 'natedev'. Never compare an AWS number with a natedev number, and never compare two numbers from different days as before and after of one change without a record on each day.

- The last AWS gate record is from 2026-11-19 09:08 UTC (AWS hold). `show.py top --host aws` reads 7 days (`TOP_MAX_AGE_DAYS=7`). Label each predicted saving `computed` or `measured`, and name the host.

### Order of work (plan-set, 2026-11-20)

Order of work: (1) orders from builds; (2) the phase with the largest AWS CPU saved per worker hour. Pass `phase N` to /unit:direct to run it. Document order has no meaning.

### One cycle

A cycle runs at each phase end. Do steps 3 and 4 at most one time each day.

1. Read the gate slowdown warnings (`slower_than_recorded`, `merge_gate.py:648` and `:1651`). natest owns them. Put each warned module in the next file.
2. Read `show.py trend --days 8` and `show.py top --limit 50` for the host that the measure rule names.
3. Do the riser check.
4. Ask builds one time for the state of each open proposal. Write each answer in the proposal table. Write the three header numbers.
5. For each phase merged to main since the last cycle, read its changed modules in `show.py top`. Write the measured saving, the host and the two dates in the next file.
6. Do the refill.
7. Choose the next phase by the order of work. Run `/unit:direct phase <N>`.

### Riser check (plan-set, 2026-11-20)

Riser check, once per cycle. Compare the module medians of the newest day with records against the lowest day in the last 7 days, same host. For each module that rose 10 CPU s or more: find the commit with `git log` on the module and its source; when the cause is in a test file, add a Lane A phase; when the cause is in source of another unit, send the commit and the two numbers to builds. When the sum over modules present on both days rose more than 5% and no module explains it, add one measure phase for the shared cost (import time, conftest, box load). Source of the 10 s and 5% values: plan-set, 2026-11-20; change them freely.

### Refill (plan-set, 2026-11-20)

Refill: at each phase end, run the riser check and `show.py top`. Never start the last todo phase before you add the next one. When the top list has no target above the floor, add a measure phase, not a stop.

- Write each new phase after the last phase, with the next integer and the Work Order fields of `~/.claude/docs/direct_plan_format.md`. Write `**Depends on:** none` or phase ids only; put each other condition in Constraints. Put the predicted saving in Goal. Nothing follows the last phase in this file.

### Lanes

**Lane A: slow tests from the top list.**
- Target: the first module in `show.py top` that has no open phase and no Floors row newer than its last owner change.
- Put modules that share one cause or one support fixture in one phase.
- Limits: every assertion stays. A cut is a cheaper start, a shared fixture, or a direct call of the real function. Never copy a production decision into a fixture. Keep a full-entry test where the decision exists only in the dispatch branch.
- A target stops at half its baseline, or at a floor result with its cause and its measure conditions in the next file. (plan-set)
- The cost report did not read each test for the "why" column of its top 40. Each phase confirms its cause first.

**Lane B: verify.sh tests.**
- Size on AWS: 1,452 CPU s in 147 modules, 25.8% of the module sum. The mac theme is 323 s in 37 modules.
- Limits: read the Floors and Declined sections of the next file first. Do not propose startup reuse or a fork server (builds, 2026-11-12 and 2026-11-16). The verify unit owns the test files; get its go through builds for each edit. A change to `verify.sh` is a proposal to its owner.
- The lane closes when Phase 401 finds no caller pair above 10 CPU s per gate. (builds, 2026-11-12)
- Join small modules only after Phase 398 closes and only with the verify unit's go.

**Lane C: fold and dead code.**
- The mission allows "removal of trivial tests" (user, 2026-11-03). The 2026-11-11 words "No test removal is approved" covered only the verify speed item and the coverage collection of that day (user, 2026-11-20).
- Lane C may do these with no question: fold sibling tests into one parametrized test with every assertion kept; delete a test only together with the dead code it tests, after the owner's answer.
- Fold only groups that share one costly start. A fold of other groups lowers the count and saves no CPU.
- Delete a test of live code when it is a duplicate, a mock-only check, a wording pin, or a row whose behavior a kept test proves. Each delete names the kept test or the reason in the summary, and gets the blind review. The value report estimates 1,350 to 1,950 such tests, from a sample of 300.

**Lane D: proposals to cut source.**
- Tests follow source. natest sends numbers to the owner through builds; natest sends no code for another owner's file. A rewrite of a file that every unit uses is the owner's decision.
- The proposal list (D1 to D4e) moves from `plan_draft.md` to the next file at instantiation. D1 waits for P7.

**Lane E: process rules.**
- The user approved P1 to P5, P6 and P7 on 2026-11-20. P8 is a proposal; it is not approved.
- builds assigns the owners. natest changes no process doc and no `assign.sh`. natest changes the gate route only in Phase 406, which builds assigned. (builds, 2026-11-20)
- Live check for one proposal: `git grep` for the rule text on origin/main. For P4, grep `assign.sh` and the fix prompt template for the rule text.
- Each natest prompt (implement, fix, test) carries the lines below now. P1 is not live; the lines bind natest workers by the work order only.

### When you write no test

1. Write a test only for a behavior that the Work Order names. A fix round may add one test for a finding that names a wrong result.
2. Write no test for a refusal, unless the refused action damages saved state.
3. Write no test for a message text, a doc, a prompt or a config wording.
4. Write no test for a helper, a fixture or test support code.
5. Write no test for a case that an existing test of the same function reaches.
6. A rare case gets a test only when the defect causes damage that no later run or command restores.
7. A test that needs a subprocess, a sleep or a real Git repository needs a reason in the summary.
8. A new test above 1 CPU s needs its cost in the phase report.
9. When your change makes a test a duplicate, name the test in the summary. Delete it in the same change.
10. "No test: <reason>" is a complete answer. A reviewer does not reopen it for coverage alone.

### Rules for every phase

1. Delete a test only by the Lane C rules: every behavior that hurts the user when it breaks stays checked. (user mission, 2026-11-03)
2. Start no test run without natemerge's go. (user through builds, 2026-11-19)
3. Keep one build-queue item for the unit at a time. (user, 2026-11-18)
4. Every phase that changes code gets a blind review. (plan-set, 2026-11-20)
5. Tests read no tunable config value; a test writes a fixture config (user, 2026-11-16). Put no hard-coded tunable in code (user, 2026-10-31).
6. Add no lazy import (plan-set; each phase since 382 carries it). Make no copy of code (user, 2026-10-30).
7. Tests run on the Mac too. (user, 2026-11-16)
8. Measure like the gate: `module_cost.py`, fresh TMPDIR on /tmp, bytecode on, no load on purpose (plan-set, 2026-11-14). Run a heavy probe on natedev under `systemd-run --user --scope -p MemoryMax=2G` (user, 2026-11-12).
9. Fix a defect where it is. A file of another owner needs that owner's go through builds. (user, decision criteria)
10. For a merge that deletes code, ask builds for a full gate. (builds, 2026-11-12)
11. Each code phase sends a main hash to builds after its review. (plan-set, 2026-11-17)

### Proposals

Full wording of P1 to P5 and P7: `report_process.md` (sections 6 and 7) and `plan_draft.md` (Lane E tracker), in the evidence folder of instantiation step 5.

| id | what | owner | sent | answer |
| --- | --- | --- | --- | --- |
| P1 | Work Order `Tests:` field; the test worker writes only the tests that it names | builds assigns | | approved (user, 2026-11-20) |
| P2 | A worker may delete a test that its change makes a duplicate; reviewers ask which added test is not needed | builds assigns | | approved (user, 2026-11-20) |
| P3 | A finding gets a test only for damage that nothing restores; a closure Minor or nit opens no round | builds assigns | | approved (user, 2026-11-20) |
| P4 | `assign.sh` adds the test rules to each implement, fix and test prompt | builds assigns | | approved (user, 2026-11-20) |
| P5 | "When you write no test" section in the worker prompt contract | builds assigns | | approved (user, 2026-11-20) |
| P6 | Affected-module merge gate on AWS, full gate each night (text below) | natest, Phase 406 (builds, 2026-11-20); aws reviews the box side | 2026-11-20 | approved (user, 2026-11-20); owner natest |
| P7 | Python review rule in `config/review/code.md`, after the last `Option<T>` bullet: a class whose only purpose is to name absence is a finding | builds assigns | | approved (user, 2026-11-20) |
| P8 | `aws_target/test_real_aws_cli.py` runs in the nightly gate only (384 natedev CPU s in 2 tests; 0.7 s on AWS) | aws, through builds | | not approved; proposal |

P6. Affected-module merge gate on AWS, full gate each night. Owner: natest, Phase 406 (builds, 2026-11-20). Today the AWS box always runs the full suite: `scripts/production/merge_gate.py:1474` sets `FullSuite("whole machine")` for a not-shared machine, and `scripts/aws_target/gate_box.py:819` records 'cloud gate'. The box has no certified base (`affected_modules.py:967`). Saving from the AWS launch log, 11-13 to 11-19 (measured on old runs, not on the new code): a merge gate run is 6.2 min of gate time and 7.3 min of box time; an affected run is 4.9 to 5.3 min of gate time; box start and setup is 1.1 min in each. So the saving is 0.9 to 1.3 wall minutes for each AWS gate, 12 to 18% of box time, and about 10 USD each month. All AWS merge gates cost about 87 USD each month (account cost, 1,075 runs in 7 days). Run time does not follow the module count: a few slow modules that almost every change selects hold the time, so Lane A work on those modules is the larger lever.

P6 risk, approved by the user on 2026-11-20: a defect outside the selected modules reaches main, and the nightly gate finds it.

### Instantiation steps

1. Before the first phase starts, send P6 to builds. Date sent: 2026-11-20. Answer: natest owns it (Phase 406).
2. Then send P1 to P5 and P7 in one message. Date sent: 2026-11-20
3. Send P8 to builds as a proposal that is not approved. Date sent: ____
4. The user answered the Lane C deletion question on 2026-11-20: the 2026-11-11 words were for one situation and are no general rule.
5. The evidence folder is `/session/natest/testcount` (adopted with `scratch.py adopt`, kept until 2026-12-20). At each refill, run `~/.claude/scripts/lib/py ~/.claude/scripts/lib/scratch.py renew /session/natest/testcount`.
6. Build the plan file in this order: title, Status, Source, Standing work, Delegation Context, `## Phases`, Phases 381 to 398, Phases 399 and up.
7. Mark Phases 382, 384 and 385 with one line each: "parked 2026-11-20: at most N AWS s; reopen from the top list". N is 19, 14 and 14. Do not park Phases 387, 394 and 395; their code is finished and waits for a gate.
8. Correct each `**Depends on:**` line of Phases 391, 392 and 393 to phase ids only. Move the other words to Constraints.
9. Move the Lane D list from `plan_draft.md` to the next file. Done 2026-11-20.
10. Send one line to builds for the owner of `docs/plans/sdlc-phases-design.md:85`: the refill tag is in `docs/direct/style_unit.md`.

### Known limits

- No rule covers source growth (+7,768 source lines each day; 4 to 12 tests for each 100 source lines). No rule covers `fix` and `feat` commits outside phases; they hold 48% of the net test growth of the last 14 days.
- New tests add about 82 CPU s to each gate each day (computed: 316 tests at 0.259 s). Only the riser check and P1 to P5 act against this.
- Not yet planned:
  - `aws_target/test_ledger.py`: 1.8 to 28.4 s on natedev on 11-20, with one test of 67.6 s. `lib/test_session_move.py`: 33 to 44 s on natedev against 4 to 5 s on AWS.
  - `review.sh` and assign_claude tests (about 200 s); unit_status tests (24 tests, 80 s). Largest single tests: copy_guard_sources 34 s; offload_cleanup 18.6 s.
  - 176 uncalled functions (1,889 lines); style_ledger migrations that already ran, and their tests.
  - Showrunner tree simplification (314 tests, 78 s); style mechanical-diff reader (628 tests, 65 s). The module split from 259 to 871 modules has no size rule.

## Delegation Context

- **Project:** `~/.claude` — the scripts, commands and config that run the user's productions. This plan records test times and lowers the CPU that the test gates use.
- **Project started:** 2026-11-03T14:50:07.684+00:00
- **Stack:** Python 3.13, standard library only (`unittest`, `sqlite3`, `json`); Markdown commands; `.conf` files read through `scripts/lib/conf.py`.
- **Layout:**
  - `scripts/test_times/` — natest owns it: `module_run.py`, `gate_record.py`, `show.py`, `module_cost.py`, `task_cpu.py`
  - `scripts/<folder>/test_*.py` — the test modules that phases change; each folder has an owner unit
  - `scripts/production/merge_gate.py`, `affected_modules.py`, `targeted_test.py` — the gate (owner: builds and natemerge); natest reads them
  - `docs/plans/builds-natest-next.md` — Floors, Declined options, the order list, measured savings, dead-code answers
- **Key files:** each line number in this plan is history from origin/main on 2026-11-20. Find the current line with `git grep`.
  - `scripts/test_times/show.py` — `top`, `trend`, `suites`, `suite`, `module`, `test`; `--host aws` selects AWS records
  - `scripts/test_times/module_cost.py` — `--tree <path> [--tests N] [--commands N] <folder>/<file> ...`: CPU, process starts, execs, wall time
  - `scripts/buildlog/index.py` — `read_only()` opens the SQLite index under `~/.local/state/buildlog/`; tables `gate_runs`, `gate_suites`, `gate_modules`, `gate_tests` (owner: report-unit)
  - `scripts/direct/verify.sh`, `scripts/direct/test_verify*.py` (owner: verify unit); `scripts/lib/shell_test_support.py` — `shell_functions` calls a real shell function from a test
- **Test lanes:** none as `tests/` folders. Each module's tests sit beside it as `scripts/<folder>/test_<name>.py`.
- **Build:** none (Python).
- **Test:** for each named test file, `python3 -m unittest discover -s scripts/<folder> -p '<file>'` from the worktree root, only after natemerge's go. A worker's verification lists every test module that calls a changed function, unchanged callers and subprocess fixtures included.
- **Lint:** `basedpyright <changed Python files>` passes when its output has the `0 errors, 0 warnings, 0 notes` line; the exit status says nothing.
- **Invariants:**
  - The rules of Standing work bind every phase.
  - Tests use fixtures. They never read or write live state: `~/.claude` main, `~/.local/state/buildlog`, `~/.local/state/merge-gate`, `/tmp/claude` (the gate's as-built).
  - Python is typed throughout, with no `Any` and no file-level type ignores (user, `~/.claude/CLAUDE.md`). All text uses Simplified Technical English (user, 2026-10-18).
  - One place for each thing (user, 2026-10-30): reuse the build log store and index, `conf.py`, `user_zone.py`, `terminal_cells.py` and the gate's `discover_test_modules`; never copy a reader, a parser or a module list. A tunable lives in config and is read at use (user, 2026-10-31).
  - Speed work keeps coverage (user mission, 2026-11-03). Every behavior a module checked before stays checked. Lane C states what a phase may delete.
  - A speed gain that needs a change outside a phase's Files goes to builds as a proposal with the measured gain; the phase does not make the change (user mission, 2026-11-03). A change to another unit's file names that owner in the checkpoint notice (production doc, Production rules).
  - A worker starts at most one detached run at a time, with at most 4 named test files (sdlc, 2026-11-03). No test run starts while a natedev build hold is in force (builds showrunner, 2026-11-03).
  - Commit every new file in the disposable gate clone before the gate starts. The aws_target box replica copies only `git ls-files`.
  - Each dispatch names one worker that runs the changed modules and their callers after the last edit.

## Phases

### Phase 381 — fast_track builds the selected tree once  · status: done (no change)

**As built:** no code change. Using the first private index tree removed 2 of 13 Git starts per selection, but two reviews found that this tree differs from `HEAD` plus `apply --cached` of the patch: selected submodules, `apply.whitespace=fix`, `diff.context` (the recovery digest changes) and diff drivers. Recovery then fails or removes a local edit. The `apply --cached` step is the check that keeps the recovery tree equal to the published patch. Branch natest-selectpatch381 keeps the attempt.

### Phase 382 — the offload cleanup test halves its AWS CPU  · status: todo
**Depends on:** none

Parked 2026-11-20: at most 19 AWS s; reopen from the top list.

#### Work Order

**Goal:** `mac_test/test_offload_cleanup.py` uses half its AWS gate CPU (37.5 s median, 38.8 s wall, 21 tests on 2026-11-19).

**Spec:**
- AWS rank 13 on 2026-11-19 03:47 EST. Every higher rank has a phase or a floor result. Slowest kept test: `MacTargetCleanupShellTests.test_remote_text_removes_only_old_abandoned_sessions_before_building` 15.2 s.
- Phase 266 limited the real lint sweep to the tests of the trim. Look for the cost that remains.
1. Measure with `module_cost.py --tests 20 --commands 20` like the gate: fresh TMPDIR on /tmp, bytecode on. Name the top commands and fixtures and their CPU share.
2. Cut the cost at its cause: repeated shell or Python starts, Git setup, fixed waits or polling. Share setup per class where the tests stay independent.
3. Name any cause in `offload.py` or the remote cleanup text; fix it there when it also costs production runs.
- Every assertion remains. Tests read no /tune-changeable value. No lazy imports, no copies, no signal handlers.
- Phase 358 (not on main) changes `test_offload.py`, a sibling module. Change shared support only where this module needs it.

**Files:** scripts/mac_test/test_offload_cleanup.py; scripts/mac_test/offload_cleanup_test_support.py; scripts/mac_test/offload.py only when the cost lives there.

**Workers:** 1 writer. One module and its support.
- impl: the test module, its support and any production cause.

**Acceptance gate:** an AWS pair (3 rounds) shows the median CPU at half or less, or a floor result with its cause; the module and the modules that import its support pass 20 times under load in a 2 GB scope; the full suite is green.

### Phase 383 — gate recovery accepts a full token pool  · status: todo
**Depends on:** none

#### Work Order
- **Goal:** A new merge gate recovers a dead gate's record when the jobserver pool is already full, and then runs its modules.
- **Spec:**
  1. Defect (found by sdlc with strace, reported by builds): a gate killed by the claude5 move left its record under `~/.local/state/merge-gate/active-runs/<repo id>/`. The jobserver (`/dev/steve`) took the dead pid's tokens back. `GateRecovery.recover` (`scripts/production/merge_gate.py`, the token loop near line 743) writes each recorded token back. The pool is full, so `os.write` raises `OSError(ENOSPC)`. The error becomes `REFUSED: [Errno 28] No space left on device`, no module runs, and the record stays. Every later gate on that copies key refuses.
  2. Fix in `recover`: an `OSError` with `errno.ENOSPC` from the token write means the token is already back. Pop the token, save the record and continue. Any other `OSError` keeps the current behavior.
  3. Then the existing folder cleanup runs on the record's folders, including a `/dev/shm/merge-gate-*` folder. Confirm that path is cleaned by the existing folder step; if it is refused, fix the cause in the same phase and say why.
  4. Smallest fix: no new layer, no retry loop, no signal handler, no lazy import.
- **Files:** `scripts/production/merge_gate.py` (recover token loop); `scripts/production/test_merge_gate_suites_signals.py` (new row).
- **Workers:** 1 writer. The fix and its test row are one small change.
  - impl: merge_gate.py and the test row.
- **Constraints from prior phases:** Phase 222 built GateRecovery. Tests read no /tune-changeable value. Fixture Git commands use `git_test_support.fixture_git_environment`.
- **Acceptance gate:**
  1. A test row: a recorded token whose write raises ENOSPC (a full pool) leaves no token in the record, removes the record file, and refuses nothing.
  2. Existing recovery rows in test_merge_gate_suites_signals.py pass.
  3. basedpyright on both files: zero errors and zero warnings.
  4. `TMPDIR=/tmp ~/.claude/scripts/lib/py -m unittest scripts.production.test_merge_gate_suites_signals` passes 20 loaded runs.

### Phase 384 — the stall watch test halves its AWS CPU  · status: todo
**Depends on:** none

Parked 2026-11-20: at most 14 AWS s; reopen from the top list.

#### Work Order
- **Goal:** production/test_stall_watch.py uses half its AWS gate CPU (AWS rank 22: 28.4 CPU s, 125 tests, 55.5 s wall; slowest test 10.3 s) with every assertion kept.
- **Spec:**
  1. Measure like the gate with module_cost.py (`--tests 20 --commands 20`), fresh TMPDIR on /tmp, bytecode on.
  2. Cut the cost at its cause: fixed waits become events the test drives; repeated process starts and Git calls become one shared fixture where tests only read it.
  3. Change stall_watch.py only when the same cost also hits production runs, and keep its safety order unchanged.
  4. Tests read no /tune-changeable value; pin each config value the fixture reads. Fixture Git commands use `git_test_support.fixture_git_environment`.
  5. If the floor is above half, report the floor and an architecture proposal.
- **Files:** `scripts/production/test_stall_watch.py`; `scripts/production/stall_watch.py` only for a production cause.
- **Workers:** 1 writer. One test module.
  - impl: test_stall_watch.py (and stall_watch.py if needed).
- **Constraints from prior phases:** AWS account blocked: measure locally until builds says AWS works. When a production change brings new safety gaps in review, restore the parent production file and keep the test savings.
- **Acceptance gate:**
  1. Local module_cost.py CPU at most half of the base.
  2. 20 loaded runs pass, TMPDIR=/tmp, 2 GB scope.
  3. basedpyright zero errors and warnings on changed files.
  4. Full natedev gate passes (review_links reds that also occur on base do not count).

### Phase 385 — the gate ledger test halves its AWS CPU  · status: todo
**Depends on:** none

Parked 2026-11-20: at most 14 AWS s; reopen from the top list.

#### Work Order
- **Goal:** production/test_merge_gate_ledger.py (AWS name test_gate_ledger.py, rank 22: 28.4 CPU s, 19 tests) uses half its AWS gate CPU with every assertion kept.
- **Spec:**
  1. Measure like the gate with module_cost.py (`--tests 20 --commands 20`), fresh TMPDIR on /tmp, bytecode on.
  2. Cut the cost at its cause: repeated repository builds, Git calls and process starts become one shared fixture where tests only read it; fixed waits become events the test drives.
  3. Change test files only. merge_gate.py is under phase 383; report a production cause as an architecture proposal.
  4. Tests read no /tune-changeable value; pin each config value the fixture reads. Fixture Git commands use `git_test_support.fixture_git_environment`.
  5. If the floor is above half, report the floor and an architecture proposal.
- **Files:** `scripts/production/test_merge_gate_ledger.py`; `scripts/production/merge_gate_test_support.py` when its callers keep passing.
- **Workers:** 1 writer. One test module and its support file.
  - impl: both files.
- **Constraints from prior phases:** AWS account blocked: measure locally until builds says AWS works. Every module that imports merge_gate_test_support.py must pass.
- **Acceptance gate:**
  1. Local module_cost.py CPU at most half of the base.
  2. 20 loaded runs of the module and the support callers pass, TMPDIR=/tmp, 2 GB scope.
  3. basedpyright zero errors and warnings on changed files.
  4. Full natedev gate passes (reds that also occur on base do not count).

### Phase 386 — the Mac fixture reachability probe has no time limit to lose  · status: todo
**Depends on:** none

#### Work Order
- **Goal:** Mac offload fixture tests pick the same target under any machine load; test_verify_mac_result_paths passes in the full gate.
- **Spec:**
  1. Defect (builds, reproduced by verify on main fb9e72058 with a forced timeout): `verify_mac_offload_test_support.py` `provider()` (near line 1075) runs the fake `ssh` stub with `timeout=1`. Under load the stub start passes 1 s, `TimeoutExpired` marks the Mac unreachable, natedev is selected, and `self.runner_events()[0]` raises IndexError.
  2. Fix at the cause: the fixture decides reachability from fixture state that the test controls, not from a timed process start. The fake `ssh` stub stays for verify.sh's own calls.
  3. A test that wants an unreachable Mac sets that state explicitly. Keep every existing row's meaning.
  4. Add a test row: the reachability answer is the same when the stub would be slow (for example, a stub that sleeps past the old limit), and the Mac is selected.
  5. Every module that imports verify_mac_offload_test_support.py keeps passing. The probe no longer starts a process, so report the CPU change of the family.
- **Files:** `scripts/direct/verify_mac_offload_test_support.py`; the test row in `scripts/direct/test_verify_mac_fixture.py` or `test_verify_mac_result_paths.py`.
- **Workers:** 1 writer. One fixture and one row.
  - impl: both files.
- **Constraints from prior phases:** Tests read no /tune-changeable value. Phase 375 (verify family) waits on the verify 43h merge; do not touch its files.
- **Acceptance gate:**
  1. The new row fails on base with the slow stub and passes on the fix.
  2. 20 loaded runs of every module that imports the support file pass, TMPDIR=/tmp, 2 GB scope.
  3. basedpyright zero errors and warnings on changed files.
  4. Full natedev gate passes (reds that also occur on base do not count).

### Phase 387 — the worktree test halves its AWS CPU  · status: todo
**Depends on:** none

#### Work Order
- **Goal:** worktree/test_worktrees.py uses half its AWS gate CPU (AWS rank 27: 27.6 CPU s, 11 tests, 28.1 s wall; slowest test 10.9 s) with every assertion kept.
- **Spec:**
  1. Measure like the gate with module_cost.py (`--tests 20 --commands 20`), fresh TMPDIR on /tmp, bytecode on.
  2. Cut the cost at its cause: repeated repository builds, Git calls and process starts become one shared fixture where tests only read it; fixed waits become events the test drives.
  3. Change worktrees.py or make_worktree.sh only when the same cost also hits production runs, and keep their safety order unchanged.
  4. Tests read no /tune-changeable value; pin each config value the fixture reads. Fixture Git commands use `git_test_support.fixture_git_environment`.
  5. If the floor is above half, report the floor and an architecture proposal.
- **Files:** `scripts/worktree/test_worktrees.py`; `scripts/worktree/worktrees.py` and `scripts/worktree/make_worktree.sh` only for a production cause.
- **Workers:** 1 writer. One test module.
  - impl: test_worktrees.py (and the production files if needed).
- **Constraints from prior phases:** AWS account blocked: measure locally until builds says AWS works. No test or profile makes load on purpose. When a production change brings new safety gaps in review, restore the parent production file and keep the test savings.
- **Acceptance gate:**
  1. Local module_cost.py CPU at most half of the base.
  2. 20 repeated runs pass, TMPDIR=/tmp, 2 GB scope.
  3. basedpyright zero errors and warnings on changed files.
  4. Full natedev gate passes (reds that also occur on base do not count).

### Phase 388 — each gate test module starts with a clean environment  · status: todo
**Depends on:** none

#### Work Order
- **Goal:** the merge gate and targeted_test.py start each test module with an environment allow list, so no module inherits caller BASH_ENV, GIT_INDEX_FILE or another caller variable (builds ask: 333 of 864 modules inherit them; inventory /tmp/claude/unit/c48c71f4-70c9-4f7b-b4bb-ce60eed7f1f4/phases/43s/impl_report_5.md, Audit inventory).
- **Spec:**
  1. Fix it in one place: `test_module_environment` in merge_gate.py (about line 1108), which both the gate and targeted_test.py use. Build the child environment from an allow list, not from the caller's whole environment.
  2. The allow list keeps what the runner and the modules need (PATH, HOME, LANG, TMPDIR and the gate's own variables). Name each kept variable and why. A module that needs a caller variable gets it from the runner, not from the caller.
  3. verify's helper `verify_test_environment` (43s) is not on origin/main. Do not take code from it. Keep one allow list in merge_gate.py; the later of the two to land switches to the other, so one helper remains.
  4. Failing first: one test row sets BASH_ENV and GIT_INDEX_FILE to traps and runs a module through the runner. It fails on the base and passes on the fix.
  5. Type check jobs keep their current environment unless the same leak reaches them.
- **Files:** `scripts/production/merge_gate.py`; one test module of the gate (`test_merge_gate_suites*.py` or `test_targeted_test.py`) for the new row; `merge_gate_test_support.py` only if the row needs it.
- **Workers:** 1 writer. One function and one test row.
  - impl: all files.
- **Constraints from prior phases:** 383 (gate recovery) changes merge_gate.py GateRecovery; 355 takes this change at its next merge of main. No test or profile makes load on purpose.
- **Acceptance gate:**
  1. The new row fails on ece29778b and passes on the fix.
  2. 20 repeated runs of the gate test modules pass, TMPDIR=/tmp, 2 GB scope.
  3. basedpyright zero errors and warnings on changed files.
  4. Full natedev gate passes (reds that also occur on base do not count).

### Phase 389 — a plan so no test can write to live folders  · status: todo
**Depends on:** none

#### Work Order
- **Goal:** a measured plan, reviewed by bob before any code, that makes every test run unable to write to live folders (user order 13:01 EST 11-19 via bob and builds, after a queue sweep test deleted 11 live /tmp/claude/unit entries at 12:46).
- **Spec:**
  1. Layer 1: the one place that starts a test step makes live roots read-only for that run and gives it its own temp folder; a wrong default fails with a write error. Candidate: a private mount view at step start (unshare); the scope at lint/invoke.sh:177 has no file rules today. Name a better form if one exists (for example systemd-run ReadOnlyPaths, Landlock).
  2. Layer 2: no delete function has a live default root; the caller gives the root. List each delete function with a default root today.
  3. Layer 3 (queue's sweep guard) is queue's; the plan names how it fits.
  4. Measured, not estimated: (a) the list of live roots (/tmp/claude, /session, ~/session, /build, ~/.claude, ~/.local/state, ~/rust/hanadocs, others found); (b) the tests that write outside their own folders today, from a full suite run under the read-only view or a write trace; (c) the form for the Mac and for AWS boxes; (d) every path where a test runs outside the step start (worker shells, merge_gate module runs, nextest, direct python -m), each as a gap; (e) the cost in worker hours and an ETA.
  5. Plan only. No production or test code until bob reviews the plan.
- **Files:** `docs/plans/test-live-folder-guard.md` (new) in the phase worktree.
- **Workers:** 1 writer (research and plan).
  - impl: the plan doc and the measurement runs.
- **Constraints from prior phases:** 388 (runner allow list) and 383 (gate recovery) change the same runner; the plan builds on them. Builds load hold: the full measured run (4b) starts only when builds says the hold ended. No run makes load on purpose.
- **Acceptance gate:**
  1. Each of 4a–4e has a measured answer with its evidence path.
  2. The plan names one form for natedev, the Mac and AWS, and each gap with its fix.
  3. bob reviews the plan before any code phase opens.

### Phase 390 — the Mac lint result survives trailing output  · status: todo
**Depends on:** none

#### Work Order
- **Goal:** a Mac-only lint run through verify.sh and offload.py reports the real lint status, not `mac=lost`, when the lint output ends with no newline or cleanup prints after it (builds ask; run 20261119T092359-117945-51e8 lost its result).
- **Spec:**
  1. Triage: /tmp/claude/unit/c48c71f4-70c9-4f7b-b4bb-ce60eed7f1f4/phases/triage_maclint/triage_report.md (result path, evidence, proposed rows).
  2. Failing first, in `scripts/mac_test/test_offload_cleanup.py`: (a) a stub lint exits 0 after output with no final newline; expect `ReportedMacStatus(0)`. (b) cleanup prints a diagnostic after the lint marker; expect the lint status and the cleanup outcome to reach the reader.
  3. Fix at the cause in `scripts/mac_test/offload.py`: the remote wrapper saves the lint status and prints one framed result after command output drains and cleanup ends. The reader takes only the framed result. A missing frame stays a refusal (`mac=lost`).
  4. Command output never sets the status; only the frame from the trusted wrapper does.
  5. `scripts/direct/test_verify_mac_only.py` covers success, failure and incomplete result.
  6. Tests read no /tune value; pin each config value the fixture reads. No signal handlers.
- **Files:** `scripts/mac_test/offload.py`; `scripts/mac_test/test_offload_cleanup.py`; `scripts/direct/test_verify_mac_only.py`.
- **Workers:** 1 writer. One protocol and its tests.
  - impl: all files.
- **Constraints from prior phases:** user test hold (13:20 11-19): write code and tests, run no test until natemerge ends the hold. Mac runs only through verify.sh and the build queue.
- **Acceptance gate:**
  1. Both new rows fail on the base and pass on the fix.
  2. 20 repeated runs of the two test modules pass, TMPDIR=/tmp, 2 GB scope.
  3. basedpyright zero errors and warnings on changed files.
  4. Full natedev gate passes (reds that also occur on base do not count).

### Phase 391 — the gate wakes on child exit and reads job memory from its cgroup  · status: dropped (2026-11-21)

**Dropped:** Phase 398 measured all gate work outside the module rows at a mean of 214 CPU s for each full natedev gate (3%). The gate polls are only a part of it. The work costs about 8 worker hours in gate.py. Sent to builds with item 80 on 11-21.
**Depends on:** Phase 392, Phase 383

Phase 392 changes the gate job start and stop. The queue item 44df is dropped (bob, List item 140): job memory comes from each module's own scope, with no gate scope.

#### Work Order
- **Goal:** merge_gate.py stops timed polls of its jobs: it wakes on child exit (pidfd) and reads each job's memory from the job cgroup, so the gate supervisor uses less CPU on every gate run (proposal from 385: child runners were 45.3% of the base trace CPU; builds accepted 11-19).
- **Spec:**
  1. Step 1, design only: measure nothing; read merge_gate.py and name each timed poll, each per-process memory sample, and the exact lines and files to change. Send builds the file list before any code (builds orders merge_gate.py work).
  2. Wake on child exit: one select.poll (or selectors) loop over the job pidfds plus the deadline; no fixed sleep.
  3. Memory: read memory.current / memory.peak of the job scope cgroup; no walk over /proc per process.
  4. Keep the gate's stop, timeout and memory-stop order unchanged; keep 383 recovery records.
  5. Failing first: a test row shows the gate makes no wake while jobs run and nothing exits (count wakes), and one shows memory comes from the cgroup file.
  6. Tests read no /tune value; no signal handlers.
- **Files:** `scripts/production/merge_gate.py` (or gate.py if 355 is on main); one gate test module for the rows.
- **Workers:** 1 writer.
  - impl: design note, then the code and rows.
- **Constraints from prior phases:** 383 recovery and 388 allow list change merge_gate.py; 355 renames it to gate.py. User test hold: no test runs until natemerge ends it.
- **Acceptance gate:**
  1. Gate supervisor CPU on a paired local gate run drops, with the number.
  2. The new rows fail on the base and pass on the fix.
  3. 20 repeated runs of the gate test modules pass, TMPDIR=/tmp, 2 GB scope.
  4. basedpyright zero errors and warnings on changed files.
  5. Full natedev gate passes (reds that also occur on base do not count).

### Phase 392 — bubble-wrapped tests on natedev for each start path  · status: todo
**Depends on:** Phase 389, Phase 383

It uses the Phase 389 plan (commit 6994708e9) and bob's review.

#### Work Order
- **Goal:** every claude_commands test on natedev runs inside one Bubblewrap launch that limits files, processes and network, or is refused (List item 141, delivery 1 of bob's reviewed route).
- **Spec:** bob's review is the spec: /home/natepiano/rust/hanadocs/bob/designs/test-container/review.md ("Recommended route", "Start paths", "Safety and the proof checks", "Detail with file:line"). Pilot: /session/natest-impl5/guard-walls/probe_walls.py:89-130 (argument list), :347-398 (proof checks).
  1. `scripts/lib/bubble_wrapped_tests.py`: `command(argv, job_folder) -> list[str]`, `guarded() -> bool` (kernel facts only: home read-only or absent, /proc/1/comm is bwrap, /proc/net/dev has only lo), `enter(argv)` (exec inside Bubblewrap or raise; refuse if still unguarded after enter). Empty root; read-only binds for /nix/store, /bin, /usr/bin, the checkout at its own path, the gate checkout, /var/tmp/dataclass-code-<uid>; private /proc, /dev, /tmp, /dev/shm, home; job folder writable; --clearenv with kept_environment; --unshare-pid --die-with-parent; --remount-ro /; MERGE_GATE_PARENT_PID=1 inside; no jobserver FIFO, no /dev/steve, no user bus.
  2. `config/bubble_wrapped_tests.conf`: keys bubblewrap, protected_folders, kept_environment (from phase 388's allow list). nate approved these names (List item 141); a further name needs nate again. `scripts/lib/direct_test_starts.py` identifies test starts for the shell hook and Python startup.
  3. No variable or flag turns the guard off. Absent Bubblewrap or a failed launch is a refusal.
  4. Gate: wrap both argument lists at merge_gate.py:1726-1729 with command(); the systemd scope prefix stays outside; SUITE_BOOTSTRAP starts with the gate interpreter and -I at :1094.
  5. module_run.py:684 and sitecustomize.py:220 call enter() (move the check before the return at :222; add test_*.py targets). coverage_overlap.py main calls enter().
  6. Proof module `scripts/lib/test_bubble_wrapped_tests.py`: the rows in bob's "Safety and the proof checks", plus phase 388's 7 leak checks and the COPIES_RUN_PID check.
  7. Hook rule in hooks/pre-tool-use-build-hold-guard.py (with build_hold/hold_enforcer.py if the parser lives there): deny a raw test start (python3 -m unittest, pytest, bash test_*.sh, node *proof*.mjs) that does not go through scripts/lib/py or bubble_wrapped_tests.py, and a test start in a checkout with no scripts/lib/bubble_wrapped_tests.py.
  8. No signal handlers, masks or dispositions; no lazy imports; tests read no /tune value.
- **Files:** worker 1: scripts/lib/bubble_wrapped_tests.py, scripts/lib/direct_test_starts.py, config/bubble_wrapped_tests.conf, scripts/production/merge_gate.py, scripts/test_times/module_run.py, scripts/lib/python_startup/sitecustomize.py, scripts/test_times/coverage_overlap.py and their test rows. Worker 2: scripts/lib/test_bubble_wrapped_tests.py, scripts/hooks/pre-tool-use-build-hold-guard.py, scripts/build_hold/hold_enforcer.py and their hook tests.
- **Workers:** 2 writers, no common file. Worker 2 codes against the bubble_wrapped_tests.py API in Spec 1.
- **Constraints from prior phases:** 383 is merged first; 355 renames merge_gate.py to gate.py (merge main before checkpoint). 388 merges no code (folded here). User test hold: write and review now; run tests only after natemerge releases units. NixOS entry for bubblewrap is asked of natedev; until then the config names the store path.
- **Acceptance gate:**
  1. The proof module passes behind the guard and fails with the guard removed (refusal row).
  2. One full natedev gate inside Bubblewrap; a failing module is fixed or reported with its reason; record the gate time with and without the guard and the cold bytecode cost (compileall pass if any loss).
  3. 20 repeated runs of the changed modules pass, TMPDIR=/tmp, 2 GB scope.
  4. basedpyright zero errors and warnings on changed files.

### Phase 393 — TERM stops a gate in a known time (no cleanup signal mask)  · status: todo

**Depends on:** none

Ordered after the red main repair and the guard repair (builds). It does not move the queue item 44dg.

#### Work Order

**Goal:** TERM always stops a merge gate and a queue place in a known time. No code blocks HUP, INT or TERM around cleanup.

**Why:** pid 101053 (merge_gate.py --tests-only) blocked TERM for 37 hours; nate needed kill -KILL. SigBlk 0x4003 is the set that `scripts/lib/cancellation.py` `defer_cleanup_signals()` blocks. The gate hung inside one such section. Candidates (not proved): `queue_place.py` `finish()` loops "while members" with no end bound; an admission call in `release` has no deadline.

**Spec:**
1. Delete `defer_cleanup_signals()` and `start_masked_thread()` from `scripts/lib/cancellation.py`. Remove every call. Callers on main a2d0e6eb6: merge_gate.py (SharedCPUSlot.release, SharedCPUPool.acquire), queue_place.py (:215, :378, :393, :495, :586), ci_cargo_front.py (:182), aws_trial.py (:292, :324, :435, :444, :516, :571), cloud_gate.py (:1014, :1446), gate_box.py (:306), admission_test_support.py (:446), test_merge_gate_suites_resources.py (:258), test_cancellation.py.
2. Each cleanup that the mask guarded is safe to run again after an interrupt at any line. Use the existing record-first pattern: write the durable record of the step before the step, and make each step a no-op when already done (a token already returned, a folder already removed, a record already gone). Recovery finishes a half-done cleanup.
3. Each wait in these paths has a deadline from existing config (reuse `config/build_admission.conf` `program_stop_seconds` or the gate deadline; add a key only if none fits, and name it in the reply). This covers `queue_place.py` `finish()` "while members" and the admission call in `release`. A deadline that passes returns an UNRESOLVED result that keeps the lease and the recovery record, as phase 392 does.
4. `CANCELLATION_SIGNALS` stays if detached_runs.py still needs it to reset dispositions in a child. The explicit UNBLOCK calls before exec (queue_place.py:207, merge_gate.py:131) stay only while an inherited mask can still reach them; say which.
5. Do not add a signal handler, mask or disposition anywhere. Do not touch staff.py, style_sync.py, cli_fixture.py. List every other mask on the gate path (module_run.py :600-680 included) in the reply with file:line and whether a wait inside it has a limit. Do not change them in this phase.
6. Tests: one test per changed path proves a hung cleanup ends on TERM in a known time. The test starts a real child process that hangs in the cleanup (a fixture wait that never ends), sends TERM, and asserts the child exits within the deadline plus a margin. Fixtures write their own config; no test reads a /tune value. Update tests that asserted the mask (test_cancellation.py, test_aws_trial.py:737, test_cloud_gate.py:2108, test_queue_place.py:1111-1123, :1607, test_merge_gate_suites_resources.py:141) to assert that no cancellation signal is blocked.

**Files:** scripts/lib/cancellation.py, scripts/lib/test_cancellation.py, scripts/production/merge_gate.py, scripts/production/test_merge_gate*.py, scripts/build_queue/queue_place.py (owner queue; claimed), scripts/build_queue/test_queue_place.py, scripts/build_queue/ci_cargo_front.py, scripts/build_queue/admission_test_support.py, scripts/aws_target/aws_trial.py, cloud_gate.py, gate_box.py and their tests.

**Workers:** 2 writers. impl8: lib, production and build_queue files (hub: cancellation.py). impl5: aws_target files and their tests, after impl8 removes the helpers (same branch; impl5 starts from impl8's first commit).

**Acceptance gate:** basedpyright zero errors and zero warnings on each changed file; git diff --check. After the test hold lifts: each changed module 20 times under load, then the full merge gate.

### Phase 394 — the review page test halves its AWS CPU  · status: todo
**Depends on:** none

#### Work Order
- **Goal:** review_links/test_review_page.py uses half its gate CPU (rank 2: 97.1 CPU s median, 130 tests, 107.9 s wall; slowest test 18.7 s, a Firefox check) with every assertion kept.
- **Spec:**
  1. Test hold: read the code and write the change now. Measure after natemerge releases units: module_cost.py (`--tests 20 --commands 20`) on base f5eb6c184 and on the result, fresh TMPDIR, bytecode on.
  2. Cut the cost at its cause: repeated Firefox starts, page renders, server starts, Git calls and process starts become one shared fixture where tests only read it; fixed waits become events the test drives.
  3. Change review page production code only when the same cost also hits production runs.
  4. Tests read no /tune-changeable value; pin each config value the fixture reads. The shared Firefox window minimum (500 px) stays.
  5. If the floor is above half, report the floor and an architecture proposal.
- **Files:** `scripts/review_links/test_review_page.py` and its test support; review page production files only for a production cause.
- **Workers:** 1 writer. One test module.
  - impl: test_review_page.py and its support.
- **Constraints from prior phases:** AWS account blocked: measure locally. No test or profile makes load on purpose. Phase 350 pinned List and status fixture settings in review_links tests; keep its pins.
- **Acceptance gate:**
  1. Local module_cost.py CPU at most half of the base.
  2. 20 repeated runs pass, TMPDIR=/tmp, 2 GB scope.
  3. basedpyright zero errors and warnings on changed files.
  4. Full natedev gate passes (reds that also occur on base do not count).

### Phase 395 — the failed probe mark test halves its AWS CPU  · status: todo
**Depends on:** none

#### Work Order
- **Goal:** mac_test/test_failed_probe_mark.py uses half its gate CPU (rank 21: 26.6 CPU s median, 21 tests, 26.3 s wall; slowest tests 4.9, 4.7 and 3.3 s) with every assertion kept.
- **Spec:**
  1. Test hold: read the code and write the change now. Measure after natemerge releases units: module_cost.py (`--tests 20 --commands 20`) on the base and on the result, fresh TMPDIR, bytecode on.
  2. Cut the cost at its cause: repeated mirror copies, shell probes, Git calls and process starts become one shared fixture where tests only read it; fixed waits become events the test drives.
  3. Change mac_test or build_targets production code only when the same cost also hits production runs.
  4. Tests read no /tune-changeable value; pin each config value the fixture reads. The test runs on the Mac too; no Linux-only entry or Mac skip.
  5. If the floor is above half, report the floor and an architecture proposal.
- **Files:** `scripts/mac_test/test_failed_probe_mark.py` and its test support; production files only for a production cause.
- **Workers:** 1 writer. One test module.
  - impl: test_failed_probe_mark.py and its support.
- **Constraints from prior phases:** AWS account blocked: measure locally. No test or profile makes load on purpose. Phase 382 changed the mac_test cleanup shim; keep its savings.
- **Acceptance gate:**
  1. Local module_cost.py CPU at most half of the base.
  2. 20 repeated runs pass, TMPDIR=/tmp, 2 GB scope.
  3. basedpyright zero errors and warnings on changed files.
  4. Full natedev gate passes (reds that also occur on base do not count).

### Phase 396 — the wait holder tests pass under load  · status: todo
**Depends on:** none

#### Work Order
- **Goal:** scripts/direct/test_verify_wait_holder.py (VerifyTargetWaitHolderTests) passes under gate load. bb6 went red at 2e7145b7b: the holder list had only the first session (the NEXT_SESSION NamedHolder was missing), and VerifyHoldTools.runTest hit "synchronous fixture hang guard expired" in selectors.select. Runs took 640-1225 s against 58 s alone.
- **Spec:**
  1. Find each place where the test or its fixture (scripts/direct/verify_hold_wait_test_support.py) depends on time: a hang guard, a deadline, a poll, or an order of events that load can change.
  2. Make each wait end on the event the code gives (the second claim's record, a pipe, a FIFO, a reap). A hang guard stays only as a last limit far above any healthy run.
  3. Fix it in the test or fixture. Change verify code only when production has the same race.
  4. Tests read no /tune value. No signal handler, mask or disposition.
- **Files:** `scripts/direct/test_verify_wait_holder.py`, `scripts/direct/verify_hold_wait_test_support.py`; verify production files only for a production race.
- **Workers:** 1 writer.
- **Constraints from prior phases:** test hold until natemerge lifts it (about 23:55 EST): static work first. Builds ranks it after 355.
- **Acceptance gate:**
  1. 20 repeated runs pass under a load like the gate's, TMPDIR=/tmp, 2 GB scope.
  2. basedpyright zero errors and warnings on changed files.
  3. Full natedev gate passes (reds that also occur on base do not count).

### Phase 397 — the step-wait interrupt test passes under load  · status: todo
**Depends on:** none

#### Work Order
- **Goal:** scripts/direct/test_verify_hold_signals.py test_interrupt_during_a_step_wait_records_seconds_and_removes_the_call passes under gate load. It hit its fixture hang guard in the sdlc restorepass gate. It passes alone on that tip and on main 10fd385ee. Phase 353 last changed it.
- **Spec:**
  1. Find each place where the test or its fixture depends on time: a hang guard, a deadline, a poll, or an order of events that load can change. Look first at `wait_for_call`, the step hold, and the TERM sent while the step waits.
  2. Make each wait end on the event the code gives (a record, a pipe, a FIFO, a reap). A hang guard stays only as a last limit far above any healthy run.
  3. Fix it in the test or fixture. Change verify code only when production has the same race; then report it before you edit.
  4. Tests read no /tune value. No new signal handler, mask or disposition.
- **Files:** `scripts/direct/test_verify_hold_signals.py` and its fixture support module; verify production files only for a production race.
- **Workers:** 1 writer.
- **Constraints from prior phases:** phase 396 made the wait holder tests wait on the new holder record; use the same event method. Builds ranks it after 355.
- **Acceptance gate:**
  1. 20 repeated runs pass under a load like the gate's, TMPDIR=/tmp, 2 GB scope.
  2. basedpyright zero errors and warnings on changed files.
  3. Full natedev gate passes (reds that also occur on base do not count).

### Phase 398 — the fixed cost of each gate module and the gate work outside the modules are measured  · status: done (no change; measure)

**As built:** no code change. Work outside the module rows: mean 214 CPU s for each full natedev gate (7 gates of 11-20 with a matched scope; range 146 to 325), not 750. Fixed cost of one module job: 0.17 CPU s (one empty module, 11-21); 840 launches are about 143 CPU s (estimate). Together about 357 CPU s, 5.5% of the 6,452 CPU s gate mean. Decision: no work on the fixed job cost; Phase 391 dropped. Result and proposal sent to builds with item 80 on 11-21.
**Depends on:** none

#### Work Order
- **Goal:** one architecture proposal with numbers: the CPU that each gate module pays before and after its tests (fixed cost), and the CPU that a full gate uses outside the module rows (about 750 CPU s of the 6,452 CPU s mean on 11-20). bob and builds accepted it under List item 80 (11-20 08:52); the module-by-module steps stop after 387, 394 and 395.
- **Spec:**
  1. Records first, no run: for each full natedev gate on 11-20, read the gate record. Give the total gate CPU, the sum of the module CPU, and the difference. Name each part of the difference that a record shows (supervisor, scopes, bytecode, setup, cleanup).
  2. Fixed cost: one module with one empty test, run the way the gate runs a module (module_run.py, the gate interpreter, sitecustomize, the scope). Then module_cost.py `--commands 20` on it and on 10 of the smallest real modules. Split the CPU by program: interpreter start, sitecustomize, imports, fixture setup, cleanup, supervisor.
  3. Outside the modules: one task_cpu.py trace of one full gate, or the 385 base trace if it is still on disk. Give the CPU of the gate supervisor, its polls (the 391 target) and each other program outside the module rows.
  4. Proposal: each change that removes a part of the fixed cost or of the outside work, with its CPU saved per gate run (measured or estimated, say which) and its cost in worker hours. Say whether 391 stays.
  5. Runs need natemerge's go and use the natest build-queue item. Fresh TMPDIR, bytecode on. No test or profile makes load on purpose. Change no repository file.
- **Files:** none (a report in the session folder).
- **Workers:** 1 writer (impl7): records step now; run steps after natemerge's go.
- **Constraints from prior phases:** 387, 394 and 395 are the last module-by-module steps. 391 waits for this result.
- **Acceptance gate:**
  1. Each number names its source run or record.
  2. The fixed cost times 840 modules plus the outside work, compared with the 6,452 CPU s gate mean.

### Phase 399 — each riser of the 11-16 to 11-19 rise has a cause  · status: done (no change)

**As built:** no code change. The report is `/session/natest/testcount/rise_report_399.md`. Mean CPU of a green AWS full gate rose 1,409 s (4,192 on 11-16 to 5,602 on 11-19). New modules: `copies/test_slot_order.py` (155.6 s, commits 2e49fd46c5 and bddbd51cf1), `review_links/test_list_page.py` (97.2, 9b2b208a45), `direct/test_verify_lint_repair_grant.py` (28.1, 891fa52702). Test file causes: `direct/test_verify_doc_checkout.py` (7.7 to 59.2, 0eeaf8919e and bddbd51cf1), `lib/test_models_registry.py` (8.1 to 26.1, 5bd2d6b09a), `worktree/test_worktrees.py` (11.2 to 27.6, 7f9f08a714). Source of another unit: statusline (2b7e107c94; one AWS gate after the Phase 365 repair gave 53.9 s), offload cleanup (aa09eeedf2), copies (bddbd51cf1). The broad rise has no cause: 798 common modules rose from 4,149 to 5,166 s, median ratio 1.135 on AWS and 1.407 on natedev. Module start explains about 19 s. No two AWS runs shared a box. A rise on both hosts points to code; Phase 407 measures it.

### Phase 400 — the List page test halves its AWS CPU  · status: todo
**Depends on:** none

#### Work Order

**Goal:** `review_links/test_list_page.py` uses half its AWS gate CPU with each main-path List behavior still checked, or has a floor result with its cause. Baseline: 97.2 CPU s median on 11-19 (measured, AWS; new on 11-17, commit 9b2b208a45). Predicted saving: about 49 CPU s per gate (computed, AWS).

**Spec:**
1. Measure first: `module_cost.py --tree <tree> --tests 20 --commands 20 review_links/test_list_page.py`, fresh TMPDIR on /tmp, bytecode on. Name the top commands and fixtures with their CPU share. The test rows are in `/session/natest/testcount/rise_report_399.md`.
2. Share one server and one browser for the module where the cases allow it (sdlc term 2). State each case that needs its own start, and why.
3. Keep one test for each main-path List behavior. Remove a browser case only when another case already proves the same behavior, for example the same check in Firefox and in Chrome (sdlc term 1).
4. Replace each fixed wait with the event that the page or the server gives.
5. Never copy a production decision into a fixture. Change no production file.
6. Write a map in the summary: each behavior, and the test that checks it after the phase. For each removed case, name the kept case that proves the same behavior.
7. If the floor is above half, report the floor, the operation that sets it and the measure conditions. Write the Floors row for the next file.

**Files:**
- `scripts/review_links/test_list_page.py` — shared starts and the removed duplicate cases

**Workers:** 1 writer. One module and its support; nothing splits.
- `impl` — all files.

**Constraints from prior phases:** sdlc gave the go on 2026-11-20 (owner: admin-console; it does not do the cut). admin-console has open edits in this file (keepplace and listadd, run B): the row merges after run B is on main, so merge main and resolve before the hash. Phase 350 pinned List and status fixture settings in review_links tests; keep them. Tests read no tunable config value. Tests run on the Mac too. One module run inside the writer pass is allowed; any other test run needs natemerge's go.

**Acceptance gate:** `module_cost.py` before and after shows CPU at half or less, labeled with its host, or the report gives a floor result; 20 repeated runs of the changed module and of each importer of changed support pass, TMPDIR=/tmp, under `systemd-run --user --scope -p MemoryMax=2G`, after natemerge's go; basedpyright gives zero errors and zero warnings on changed files; one blind review; the gate route that builds names for the merge passes. After the phase is merged to main, the director writes the AWS median in the next file when AWS has a full gate in the last 2 days.

### Phase 401 — the start cost of one verify.sh test is measured  · status: done (measure)

**As built:** measurement only, no code change (natedev, main `6401c0f801`, run `20261121T191825-1684979-25d4`). Result: "none does; the verify lane closes". The largest caller pair, `test_verify_mac_only.py` and `test_verify_mac_local.py`, makes 32 full `verify.sh` starts at about 4.9 CPU s per gate. All 54 tests in the ten modules passed. The floor row is in the next file.
**Depends on:** none

#### Work Order

**Goal:** one report splits the CPU of a verify.sh test into start cost and test work, and states whether one caller pair shows more than 10 CPU s per gate. Predicted saving: 0 CPU (measure). The result sets the target of Phase 402 or closes the verify lane.

**Spec:**
1. Read the Floors and Declined sections of `docs/plans/builds-natest-next.md` first. Do not propose startup reuse or a fork server.
2. Records first, no run. From `show.py module <folder>/<file>` for 10 verify modules, give module CPU, tests, and CPU for each test. Take the 3 one-test final-pass modules, 3 mac modules, 2 lint modules and 2 token modules.
3. After natemerge's go, run `module_cost.py --tree <tree> --tests 20 --commands 20` on the same 10 modules. Split the CPU of each test: Python fixture setup, each fake tool start, `bash verify.sh` up to its first step decision, the step, cleanup.
4. Measure the smallest start alone: one `verify.sh` call that stops at an argument refusal, 20 times, with the same fixture environment.
5. Count the full-script starts for each test in the mac, lint and token themes.
6. For each theme, name the starts that a direct call of the real function through `shell_test_support.shell_functions` can replace. Name the starts that only a real process path can prove.
7. The report must show more than 10 CPU s per gate in one caller pair, or the verify lane closes. State the pair, the two numbers and the host.
8. When the lane closes, write one line for the next file, and the director drops Phase 402 with that line.
9. When the lane stays open, give the target for the mac theme in CPU s, with the measure behind it.
10. Change no repository file.

**Files:**
- `docs/plans/builds-natest-next.md`, `scripts/direct/verify.sh`, `scripts/direct/test_verify*.py`, `scripts/lib/shell_test_support.py` — read only; no file changes, and the report goes in the session folder of this phase

**Workers:** 1 writer. Records step at once; run steps after natemerge's go.
- `impl` — the report.

**State:** none.

**Constraints from prior phases:** Verify tests on AWS: 1,452 CPU s in 147 modules. Recorded floors: "The tested verify harness changes are at a practical floor" (Phase 190). Phase 179 measured 496 full-script launches in 338 of 414 slow verify tests; at least 32 tests need the real process path. Phase 157 floor for `test_verify_mac_only.py`: 36 to 38 CPU s in AWS pairs. Past verify phases saved 0.4 to 11 s each; Phases 181 and 183 saved 1.37 s and 0.71 s, because the full entries held most of the cost. Declined: startup reuse (builds, 2026-11-12; reopen only above 10 CPU s per gate in a measured caller pair) and a preloaded Python fork server (builds, 2026-11-16). Use Phase 398's split of one module start if its report exists. The verify unit owns the verify files.

**Acceptance gate:** each number names its run or its record, with host; the report states one of two results: "one caller pair shows more than 10 CPU s per gate" with the pair, or "none does; the verify lane closes"; `git status` shows no changed file.

### Phase 402 — the verify mac theme reaches the target of Phase 401  · status: dropped

**As built:** dropped. Phase 401 found no caller pair above 10 CPU s per gate, so the verify lane closes (Floors row in the next file).
**Depends on:** Phase 401, Phase 386, Phase 390

#### Work Order

**Goal:** the mac theme of the verify.sh tests uses the AWS gate CPU that Phase 401 sets as target, with every assertion kept. Baseline: 323 s AWS (37 modules, 102 tests; measured medians, 11-19). Predicted saving: target set by Phase 401; no number before that measure.

**Spec:**
1. Take the route that the Phase 401 report names. Write its target into Goal before dispatch.
2. Route 1, start cost holds the CPU: a test of one decision calls the real verify function through `shell_test_support.shell_functions`. Each module keeps its full-entry tests for dispatch and argument wiring.
3. Route 2, test work holds the CPU: share one fixture start for each class where the tests only read it. Replace each timed wait with the event that the code gives.
4. Keep a full-entry test where the decision exists only in the dispatch branch. Never copy the decision into a fixture.
5. A missing callable boundary in `verify.sh` is a proposal to the verify unit through builds. Do not edit `verify.sh` in this phase.
6. Add no test function. Map each assertion to the test that checks it after the phase; put the map in the summary.
7. Measure with `module_cost.py` before and after, like the gate.

**Files:**
- `scripts/direct/test_verify_mac_*.py` — the cheaper start
- `scripts/direct/verify_mac_offload_test_support.py` — shared fixture start

**Workers:** 2 writers, split by module list: the first 19 mac modules by name, and the other 18.
- `impl` — the first 19 modules; hub: `scripts/direct/verify_mac_offload_test_support.py` (each mac module imports it)
- `test` — opens as impl; the other 18 modules

**Constraints from prior phases:** The verify unit owns these files: get its go through builds for each file before the edit. Phase 386 changes `provider()` in `verify_mac_offload_test_support.py`, and Phase 390 changes `test_verify_mac_only.py`; start from a tip that holds both. Floor for `test_verify_mac_only.py`: 36 to 38 CPU s (Phase 157). Do not propose startup reuse or a fork server. Tests run on the Mac too. No test run starts without natemerge's go.

**Acceptance gate:** the `module_cost.py` sum of the 37 modules is at the Phase 401 target or less, labeled with its host, or the report gives a floor result with its cause; 20 repeated runs of each changed module and of each importer of the support file pass, TMPDIR=/tmp, under `systemd-run --user --scope -p MemoryMax=2G`; basedpyright gives zero errors and zero warnings on changed files; one blind review; the gate route that builds names passes. After the phase is merged to main, the director writes the AWS sum in the next file when AWS has a full gate in the last 2 days.

### Phase 403 — sibling tests that share one costly start fold into one test  · status: done (no change)

**As built:** no code change (main 9f3459ec1b, 2026-11-21). A source read of the four groups found no read-only shared start, so Spec step 3 stops each fold. Gate cleanup: each signal case needs a fresh failed-gate process. Lint cache: each tool case changes its version and reads new cache results. SSH refusal: each case is a separate verify call that writes claim and attempt records. Unit status: each scenario uses its own registry or pane sequence, and the status command writes decisions to the fixture. No measurement ran. A later fold needs a group whose rows only read one costly start.
**Depends on:** none

#### Work Order

**Goal:** each named group of sibling tests is one parametrized test that pays its costly start one time, with every assertion kept. Predicted saving (computed, AWS): at most 18 CPU s per gate for `test_unit_status_decisions.py` (7 of 8 starts of 2.6 s). The other three groups have no number before step 1.

**Spec:**
1. Fold only groups that share one costly start. First groups:
   - `scripts/production/test_merge_gate_suites_signals.py:291-300` — four tests call `assert_gate_folder_removed` with "failure", "SIGTERM", "SIGHUP", "SIGINT" (module: 23.9 AWS s).
   - `scripts/direct/test_verify_lint_cache_versions.py:122-175` — one test for each tool: mend, rustfmt, clippy, taplo (module: 14.9 AWS s).
   - `scripts/direct/test_verify_ssh_targets.py:34-40` — three tests for a refusal at three points (module: 18.8 AWS s).
   - `scripts/production/test_unit_status_decisions.py` — 8 tests, 20.9 AWS s through one fixture.
2. Before each fold, measure the group with `module_cost.py --tests 20 --commands 20`. Name the start that the rows share.
3. Fold a group only when its rows read one start and do not change it. When each row needs its own start, leave the group and say so in the summary.
4. Use one test with a `subTest` table. The old test name is the row label. Build the shared start one time for the table.
5. Keep every assertion of every old test. Delete no case.
6. Write no new tool and no new test function.
7. Check 3 folded rows: break one line of the code under each row on a temporary copy, and see that row fail.

**Files:**
- `scripts/production/test_merge_gate_suites_signals.py`, `scripts/production/test_unit_status_decisions.py` — folds
- `scripts/direct/test_verify_lint_cache_versions.py`, `scripts/direct/test_verify_ssh_targets.py` — folds

**Workers:** 1 writer. Four small groups; nothing splits.
- `impl` — all files.

**Constraints from prior phases:** Each file's owner gives its go through builds before the edit: the verify unit for the two `direct` files, the owner that builds names for the two `production` files. A fold keeps every assertion. A fold saves CPU only when the rows share one start (value report). Tests read no tunable config value. No test run starts without natemerge's go.

**Acceptance gate:** each old test name is a row label in the new tables (compare the names before and after); the 3 broken-code checks of step 7 fail as stated; `module_cost.py` CPU of each changed module is not above its base, with both numbers and the host; 20 repeated runs of each changed module pass, TMPDIR=/tmp, under `systemd-run --user --scope -p MemoryMax=2G`; basedpyright gives zero errors and zero warnings on changed files; one blind review.

### Phase 404 — each dead-code candidate has its owner's answer  · status: done (no code change)

**As built:** 30 candidates on main 9f3459ec1b; every owner answered on 2026-11-21 (section "Dead-code answers" in `docs/plans/builds-natest-next.md`). Delete: 21 files in 9 owner groups. Keep: 8 (`roles_audit.py`, `worker_usage.py`, `phase_process.py`, `install_codex_agents.py`, `coverage_overlap.py`, and three style reader tools that the cargo-liner repository runs). The audit missed the cargo-liner callers; later audits read `~/rust/cargo-liner` as a root. Evidence: the session folder of this phase (`phases/404/evidence.md`).
**Depends on:** none

#### Work Order

**Goal:** each dead-code candidate has an evidence row and a recorded answer from its owner: delete, keep, or not yet. Predicted saving: 0 CPU (proposal and tracking). The answers set the list of Phase 405.

**Spec:**
1. This phase changes no code. Build the evidence table on present origin/main with an AST import graph and a text search of each file name.
2. Roots of the graph: `commands/`, `config/`, `settings.json`, shell scripts, `/etc/nixos`, `~/.config/systemd/user`, `~/.codex/hooks.json`, and other repositories that run `scripts.*` modules (`~/rust/cargo-liner`: its tests and generated files call three style tools; cargo-liner, 2026-11-21). `docs/plans` and `docs/as-built` are not roots.
3. Group 1 (folders report: 164 tests, 60.6 CPU s): `style/selection_capture.py`, `style/refill_measure.py`, `direct/demo_refusals.sh`, `production/unit_run.py`, `style/order_waste.py`, `style/style_reader_tables.py`, `style/style_reader_oracle.py`, `style/style_reader_parity.py`, `direct/phase_process.py`, `production/roles_audit.py`.
4. Group 2 (size report tier A, 5,337 lines): `lib/session_move.py` (changed 2026-11-19; ask first), `whoami/profile_notes_pass.py`, `whoami/worker_usage.py`, `usage/compaction_replay.py`, `lint/gate_folders_delete.py`, `agents/pane_restore.py` with `hooks/session-end-pane-record.py`, `mac_test/loopback_proof.py`, `lint/package_imports.py`, `statusline/statusline_rows.py`, `aws_target/terminal_table.py`.
5. Group 2, tier B (2,957 lines, low to medium confidence): `mac_test/mac_gate.py`, `nix_env/heal.py`, `berth/work_order.py`, `test_times/coverage_overlap.py`, `accounts/pane_moves.py`, `message/repeats.py`, `history/routed.py`, `message/install_codex_agents.py`.
6. Group 3, trials and one-time tools: `aws_target/gpu_size_trial.py`, `gpu_trial_wrapper.py`, `aws_trial.py`, `gate_probe.py`, `measurement.py` (`deploy.py` imports three of them), `buildlog/width_trial.py` (`lint/invoke.sh` calls it), `model_study/`, `issue_score/experiment.py`.
7. Group 4, the style bulk batch path: `style/crate_run.py`, `style/bulk_run.py`, `style/bulk_constants.py`, most of `style/bulk_rename.py`. Keep `bulk_rename.cargo_output`; `style_plan.py` uses it. The style owner must answer.
8. For each row give: lines, test files, tests, the median CPU of its test modules with host, the evidence, and one question.
9. Send the rows to builds for routing to each owner. Record only the owner's own answer, with its date and who gave it.
10. `test_times/coverage_overlap.py` is natest's file. Decide it in this phase and state the reason.

**Files:**
- `docs/plans/builds-natest-next.md` — new section "Dead-code answers"; the evidence table goes in the session folder of this phase

**Workers:** 1 writer. One graph, one table, one set of questions.
- `impl` — the table, the question texts and the new section.

**State:** the section "Dead-code answers" in `docs/plans/builds-natest-next.md`.

**Constraints from prior phases:** The pilot scripts `size_graph.py`, `size_reach.py` and `folders_dead.py` are in the evidence folder of instantiation step 5; read them, do not copy them into the repository. Totals from the reports (size report rows 3, 4, 7, 8): about 19,700 source lines, 24,000 test lines and 1,005 tests.

**Acceptance gate:** each candidate has a row with evidence and one question; each sent question has its send result line; each answer in the next file names who gave it and the date; no code file changed.

### Phase 405 — dead code with a "delete" answer is deleted with its tests  · status: todo
**Depends on:** Phase 404

#### Work Order

**Goal:** each file that its owner answered "delete" in Phase 404 is gone from main with its tests, test support and config keys. Predicted saving: at most 60.6 CPU s per gate for group 1 (measured medians, folders report, natedev). At most 83.0 AWS CPU s more if the style owner answers "delete" for `style/test_crate_run.py` (49.3) and `style/test_bulk_run.py` (33.7) (measured medians, AWS, 11-19).

**Spec:**
1. Take only the rows with a recorded "delete" answer in the section "Dead-code answers". Make one commit for each owner group.
2. Delete the source file, its `test_*.py` files, and each support file that has no other importer.
3. Delete its config keys and their `config/README.md` lines. Before each key delete, grep every reader of the key and show that none remains.
4. Correct each as-built doc line that names a deleted file, in the same commit.
5. Where a kept file imports a deleted one (`deploy.py` imports `aws_trial` and `gate_probe`; `lint/invoke.sh` calls `width_trial.py`), remove the call only with that owner's answer. Without the answer, keep the file and say so.
6. Delete a test only together with the dead code it tests.
7. Summary: each deleted test file with the reason "code deleted".

**Files:**
- `scripts/` — the files of the "delete" rows; the director writes the list here at dispatch
- `config/*.conf`, `config/README.md`, `docs/as-built/*.md` — the lines that name them

**Workers:** 2 writers, split by folder group so that no file is common.
- `impl` — the style, direct and production rows; hub: `config/README.md` (each key delete edits it)
- `test` — opens as impl; the rows of the other folders

**State:** file deletes; config key deletes.

**Constraints from prior phases:** Phase 404 holds the answers; an answer of "keep" or "not yet" is final for this phase. For a merge that deletes code, ask builds for a full gate (rule 10): a delete on 11-12 with touched tests only broke 10 modules on main. A deleted config key goes to builds for the showrunner's go first. No test run starts without natemerge's go.

**Acceptance gate:** a grep of each deleted name on the tree finds no reader; the full gate that builds runs for the merge passes on the tip; basedpyright gives zero errors and zero warnings on changed files; one blind review. After the phase is merged to main, the director writes the CPU of the removed modules, with host, in the next file.

### Phase 406 — an AWS merge gate runs the affected modules, and one full gate runs each night  · status: dropped (user 2026-11-21: "i want merge to do a test all"; the merge gate keeps the full suite; branch natest-affected406 is not merged)
**Depends on:** none

#### Work Order

**Goal:** an AWS merge gate runs only the modules that the change affects, and one full gate runs each night. Predicted saving (AWS launch log, 11-13 to 11-19): 0.9 to 1.3 wall minutes of a 7.3 minute box run for each AWS gate, and about 10 USD each month.

**Spec:**
1. Start from a tip that holds Phase 355 (the gate code is `scripts/production/gate.py` there). Find each current line with `git grep`.
2. Today a not-shared machine sets `FullSuite("whole machine")`, `scripts/aws_target/gate_box.py` records 'cloud gate', and the box has no certified base (`affected_modules.py`, "no certified base").
3. natedev selects the affected modules against its certified base with `affected_modules.py`. Use the selection that the shared path already uses; write no second selector.
4. The gate request to the box carries the module list. The box runs that list and records the selection reason and the module count in its `gate_run` record.
5. When natedev has no certified base, or the selector returns the full suite, the box runs the full suite as today.
6. One full gate runs each night on the tip of main. First find the nightly job that exists (`git grep -n nightly scripts config`); add the full gate to it and write no second scheduler.
7. A red nightly full gate uses the existing nightly red route, so that it gets an assignee. The summary states how the nightly gate starts and where its red goes; the director sends this to builds before the hash.
8. A nightly red in a module that no merge gate of that day selected is written to the record with the list of merges of that day.
9. Each new tunable (the nightly time, any limit) is a key in config, read at use. Tests write a fixture config.
10. Tests: one test that a request with a module list runs only that list on the box; one that no certified base gives the full suite; one that the nightly job starts a full gate; one that a nightly red reaches the red route. Use the gate's fixture; start no real box.

**Files:**
- `scripts/production/gate.py`, `scripts/production/affected_modules.py` — selection for a not-shared machine
- `scripts/aws_target/gate_box.py`, `scripts/aws_target/cloud_gate.py` — the request field and the box run (aws reviews this side)
- `scripts/production/`, `config/` — the nightly job file that step 6 finds, its config, and the test files
- `docs/as-built/` — the lines that say each merge gate is full

**Workers:** 2 writers, split by folder.
- `impl` — `scripts/production` and the nightly job; hub: the request record type
- `test` — opens as impl; `scripts/aws_target`

**Constraints from prior phases:** builds made natest the owner on 2026-11-20, because Phase 355 renames the gate code and one owner keeps the order. The user approved the change and its risk on 2026-11-20: a defect outside the selected modules reaches main, and the nightly gate finds it. The change is merged to main only after Phase 355 is on main. The box result reader merges before the writer: natedev reads with main and the box writes with candidate code, so keep the record readable by both. aws reviews the `gate_box.py` and `cloud_gate.py` side before the hash. No test run starts without natemerge's go.

**Acceptance gate:** the four tests pass in 20 repeated runs with each importer of changed code, TMPDIR=/tmp, under `systemd-run --user --scope -p MemoryMax=2G`; basedpyright gives zero errors and zero warnings on changed files; one blind review and the aws review have no open finding; the full gate that builds runs for the merge passes. After the phase is merged to main, the director writes in the next file the mean modules for each AWS merge gate and the count of nightly reds that no merge gate selected.

### Phase 407 — the broad CPU rise in unchanged modules has a measured cause  · status: done (no change; measure)

**As built:** no code change. Six unchanged modules, tree e7670cf8d7 to b576bf26b5 on natedev: 15.9 to 19.3 CPU s (+21%), with the same process starts and execs. The rise is in Python programs: module_run.py 0.08 to 0.12 for each module; update_registration.py 0.96 to 2.33 and 0.24 to 0.73; python3 -c 3.09 to 3.97. The run does not isolate one commit. Candidates: 70e6c54344 (changed python_startup/sitecustomize.py and module_run.py), 51a28f5e8c (module_run.py), 6fc8f9c49c (Review imports in update_registration.py). Phase 412 measures the Python start cost.
**Depends on:** none

#### Work Order

**Goal:** one report names the code change that made unchanged modules use more CPU from 11-16 to 11-19, or shows that none did. Predicted saving: 0 CPU in this phase (measure). The broad rise is about 1,018 CPU s for each AWS full gate (measured, AWS: 798 common modules, 4,149 to 5,166 s).

**Spec:**
1. Read `/session/natest/testcount/rise_report_399.md`, sections 3 and 7. Section 7 holds the run commands.
2. Take 6 modules whose test files and source did not change in the window and whose AWS median rose more than 20%: 3 that start many shell processes and 3 that start none.
3. Run each with `module_cost.py --tests 20 --commands 20` on the tree of the last 11-16 gate and on the tree of the last 11-19 gate, in private clones, fresh TMPDIR on /tmp, bytecode on.
4. Compare for each module: CPU, process starts, execs, and CPU for each command group. Name the command group that holds the rise.
5. When a group holds the rise, find the commit with `git log` between the two trees on the files that the group runs. Candidates from Phase 399: 51a28f5e8c and 70e6c54344 (runner folders from the leased allocator), and shell start files.
6. When natedev shows no rise between the two trees, state that the cause is outside the tree (box image, kernel, load) and list what the records can and cannot show.
7. Change no repository file.

**Files:**
- `scripts/test_times/module_cost.py`, `scripts/test_times/module_run.py` — read and run only; no file changes, and the report goes in the session folder of this phase

**Workers:** 1 writer. One measure; nothing splits.
- `impl` — the runs and the report.

**State:** none.

**Constraints from prior phases:** Phase 399 ruled out a fixed module start cost (about 19 s of the rise) and shared boxes on AWS. The runs need natemerge's go, the unit's one build-queue item, and `systemd-run --user --scope -p MemoryMax=2G`. Never give a natedev number as an AWS number.

**Acceptance gate:** the report gives both numbers for each of the 6 modules with host and tree; it names one commit with the command group that holds the rise, or states "no rise between the trees on natedev" with the numbers; `git status` shows no changed file.

### Phase 408 — the lint repair grant test halves its AWS CPU  · status: done (no change; floor)

**As built:** no code change. The module is at its floor: 29.1 CPU s on natedev (base e332b0e70b). The trial (commit 558f820cd2, not merged) moved three grant rejection cases to direct lint function calls and saved 2.2 CPU s (7.7%). The blind review found that the direct call skips rustfmt and the Taplo check, so the exit status assertion proved less. The trial also named 19 internal functions of `verify.sh` in the fixture. Ruling: 2.2 s does not pay for less coverage and that coupling; the trial is dropped. The Floors row is in the next file. The cost is in production starts: the verify helper and the admission clients (13.9 CPU s) and the module runner (6.6).

**Depends on:** none

#### Work Order

**Goal:** `direct/test_verify_lint_repair_grant.py` uses half its AWS gate CPU with every assertion kept, or has a floor result with its cause. Baseline: 28.1 CPU s median on 11-19 (measured, AWS; new module, commit 891fa52702). Predicted saving: about 14 CPU s per gate (computed, AWS).

**Spec:**
1. Measure first with `module_cost.py --tree <tree> --tests 20 --commands 20 direct/test_verify_lint_repair_grant.py`. Name the top commands and fixtures with their CPU share.
2. Cut the cost at its cause: repeated full-script starts, Git setup, fixed waits. Call the real verify function through `shell_test_support.shell_functions` where the decision is in a function.
3. Keep every assertion. Keep a full-entry test where the decision exists only in the dispatch branch. Never copy a production decision into a fixture.
4. Add no test function. Map each assertion to the test that checks it after the phase; put the map in the summary.
5. If the floor is above half, report the floor and its cause, and write the Floors row for the next file.

**Files:**
- `scripts/direct/test_verify_lint_repair_grant.py` — the cheaper start

**Workers:** 1 writer. One module; nothing splits.
- `impl` — the file.

**Constraints from prior phases:** builds routed this cut to natest on 2026-11-20 (the file came from the queue item 44bk; builds tells queue). The Lane B limits apply: read the Floors and Declined sections of the next file first; do not propose startup reuse or a fork server. Tests run on the Mac too. One module run inside the writer pass is allowed; any other test run needs natemerge's go.

**Acceptance gate:** `module_cost.py` before and after shows CPU at half or less, labeled with its host, or the report gives a floor result; 20 repeated runs of the changed module pass, TMPDIR=/tmp, under `systemd-run --user --scope -p MemoryMax=2G`, after natemerge's go; basedpyright gives zero errors and zero warnings on the changed file; one blind review.


### Phase 409 — a rerun or bisect step stops a hanging module after a short limit  · status: todo
**Depends on:** none

#### Work Order

**Goal:** a rerun step or a bisect step of the merge gate stops a module that hangs after a limit made from that module's measured time, not after 1800 s. On 2026-11-20 one hanging test cost 30 minutes in each bisect step of batch bb13 (natemerge's finding, assigned by builds). The slowest measured module takes 387 s.

**Spec:**
1. Find each path where a rerun step or a bisect step runs named gate modules: `run_named_modules` in `scripts/production/merge_gate.py`, the memory-stop rerun in `run_suites`, and the red-step rerun and `bisect_red_batch` calls in `scripts/production/merge_checkpoint.py` through `checkpoint_gate.narrowed` and `checkpoint_gate.run_test_steps`. State each path in the summary.
2. For these paths, each module gets its own limit: `max(rerun.module_time_floor_seconds, rerun.module_time_factor × median wall time of the passing runs of that module on the same vCPU count)`. Read the medians with the query that `gate_record.history_time_limit` already uses; share that code, do not copy it.
3. A module with no passing record keeps `DEFAULT_MODULE_TIME_LIMIT_S`. A full gate run and a nightly run keep `DEFAULT_MODULE_TIME_LIMIT_S`. An explicit `--module-time-limit-s` argument still sets the limit for every module.
4. Add two keys to `config/merge_gate.conf` with their range comments: `rerun.module_time_factor=3.0` (number, range 1..) and `rerun.module_time_floor_seconds=120` (whole, 1..). Read them through `conf.read("merge_gate")`. No number of the two is in code.
5. The kill text names the limit and its source: the factor and the median, or the floor.
6. Model the limit as a type: a per-module limit that is one of measured, floor or default. Do not pass a bare optional float.
7. Tests: table rows in the existing merge gate suite tests. Rows: a module with history gets factor × median; a short module gets the floor; a module with no history gets the default; a full run keeps the default; a hanging module in a named-module run is stopped at its limit (use a fixture config with a small floor and a fixture module that sleeps; the test reads no live config value). Each test fails without the change.

**Files:**
- `scripts/production/merge_gate.py` — the per-module limit for named-module runs
- `scripts/production/merge_checkpoint.py` — the rerun and bisect calls, only if they need a change
- `scripts/production/checkpoint_gate.py` — the narrowed step, only if it needs a change
- `scripts/test_times/gate_record.py` — the shared module median read
- `config/merge_gate.conf` — the two keys
- `scripts/production/test_merge_gate_suites_reruns.py` — the table rows
- `scripts/test_times/test_gate_record.py` — rows for the shared read, if it moves

**Workers:** 1 writer. The files depend on one another; nothing splits.
- `impl` — all files.

**State:** none. The change reads the gate index and writes no record.

**Constraints from prior phases:** the base is main. Phase 355 renames `merge_gate.py` to `gate.py`; it follows this change by rename at its next merge of main. The report unit's branch `builds-report-jobnames` changes one line in the merge gate test files; keep edits away from the first lines of those files. Tests read no tunable config value. Tests run on the Mac too. Focused modules inside the writer pass are allowed; any other test run needs natemerge's go.

**Acceptance gate:** basedpyright gives zero errors and zero warnings on changed files; the changed test modules pass; 20 repeated runs of each changed test module pass after natemerge's go; one blind review; the gate route that builds names for the merge passes.

### Phase 410 — a named module run on the AWS box or the Mac gets the short limit  · status: todo
**Depends on:** Phase 409

#### Work Order

**Goal:** a rerun step or a bisect step that the cloud route or the Mac route runs stops a hanging module after the Phase 409 limit, not after 1800 s. Phase 409 gives the limit only to runs on natedev.

**Spec:**
1. State where a named module run goes when the cloud route is on: `merge_gate.py` near the `cloud.route` call, `scripts/aws_target/gate_box.py` and `scripts/mac_test/mac_gate.py`.
2. The box and the Mac have no gate index. natedev makes the per-module limits with `read_rerun_module_time_limits` and sends them in the request. The key is the vCPU count of the machine that runs the modules.
3. The box and the Mac read the limits when the request has them. A request without them keeps `module_time_limit_s`. The reader merges to main before the writer, because the box runs the candidate code and natedev runs main.
4. A module with no limit in the request keeps `module_time_limit_s`. A full gate run sends no per-module limits.
5. Tests: table rows in the existing request and box tests. Rows: a request with limits stops a hanging fixture module at its limit; a request without limits keeps the old limit; an old reader accepts a new request.

**Files:**
- `scripts/production/merge_gate.py` — the request for the cloud route
- `scripts/aws_target/cloud_gate.py` — the request field
- `scripts/aws_target/gate_box.py` — the reader on the box
- `scripts/mac_test/mac_gate.py` — the reader on the Mac
- `scripts/aws_target/test_gate_box.py` — the reader rows; the other test modules of these files as the writer finds them

**Workers:** 2 writers. The split is reader and writer.
- `impl` — `gate_box.py`, `mac_gate.py` and their tests (the reader commit).
- `test` opens as impl — `merge_gate.py`, `cloud_gate.py` and their tests (the writer commit). hub: `cloud_gate.py`.

**State:** one new optional request field. No stored record changes.

**Constraints from prior phases:** Phase 409 made `ModuleTimeLimit`, `read_rerun_module_time_limits` and the two keys `rerun.module_time_factor` and `rerun.module_time_floor_seconds`. Phase 355 renames `merge_gate.py` to `gate.py`. Phase 406 is held and also adds request fields; merge-tree against `natest-affected406` before the hash.

**Acceptance gate:** basedpyright gives zero errors and zero warnings on changed files; the changed test modules pass; one blind review; the reader hash goes to builds before the writer hash.

### Phase 411 — a merge batch runs the tests its rows affect before the full run  · status: todo
**Depends on:** Phase 409

#### Work Order

**Goal:** a merge batch finds a red row in minutes. On 2026-11-20 four red rows (44dz, 178, 45e, 45g) each cost about 1 h of full run. Order from builds (proposal of bob, with the support of natemerge).

**Spec:**
1. Design note first (read only): the step list in `scripts/production/checkpoint_gate.py`, the batch flow in `scripts/production/merge_checkpoint.py`, and the affected-module map (`select_affected` and `select_targeted_modules` in `scripts/production/merge_gate.py`, `scan_roots` in `config/merge_gate.conf`). The note states where the new step goes, how a red row leaves, and each gap of the map.
2. Before the full test step of a batch, a new step runs the test modules that the rows of the batch affect. It uses the existing map; it copies no selection code.
3. A row that is red in this step leaves the batch through the existing red-row path. The full test step then runs once on the tree that passed.
4. The map gives the tests for doc rows and staff rows. On 2026-11-20 it missed `test_staff_accessors` for a staff row. A row with no mapped test adds no module; the full step still covers it.
5. The new step is a filter, not a proof. The full step stays the only step that lets a batch merge. Nothing weakens it.
6. Modules in the new step use the Phase 409 module limits for named runs.
7. Tests: table rows in the existing checkpoint tests. Rows: a red row leaves before the full step and the full step runs once; a batch with all rows green runs the new step and then the full step; a batch with no affected module skips the new step; a staff row and a doc row select their tests.

**Files:**
- `scripts/production/checkpoint_gate.py` — the step list and the new step
- `scripts/production/merge_checkpoint.py` — the batch flow
- `scripts/production/merge_gate.py` — the map, only where it has a gap
- `config/merge_gate.conf` — map rows, only where the map has a gap
- `scripts/production/test_merge_checkpoint_rerun.py` — table rows; the other checkpoint test modules as the design note names them

**Workers:** 2 writers after the design note. The split is the step and the map.
- `impl` — `checkpoint_gate.py`, `merge_checkpoint.py` and their tests. hub: `checkpoint_gate.py`.
- `test` opens as impl — the map gaps in `merge_gate.py`, `config/merge_gate.conf` and their tests.

**State:** none new, unless the design note shows that the batch record must name the new step.

**Constraints from prior phases:** Phase 409 changed `only_tests` and the module limits; the base is main after 409. Phase 355 renames `merge_gate.py` to `gate.py`; merge-tree against the 355 tip before the hash. Phase 406 (held) sends an affected selection to the AWS gate; this phase does not depend on it and changes no AWS request. natest changes no process doc. Tests read no tunable config value.

**Acceptance gate:** basedpyright gives zero errors and zero warnings on changed files; the changed test modules pass; one blind review; 20 repeated runs of each changed test module after natemerge's go; the gate route that builds names for the merge passes.

### Phase 412 — the CPU cost of one Python start in a gate has a measured cause  · status: done (no change; measure)

**As built:** no code change. One Python start costs 19.7, 19.7 and 19.6 CPU ms on e7670cf8d7, b576bf26b5 and main 262952c406 (natedev, 200 starts): the start is not the cause of the rise. An empty gate job rose from 120 to 151 CPU ms: 51a28f5e8c adds 11 ms and 70e6c54344 adds 24 ms (leased folders in module_run.py), about 29 CPU s for each gate. One gate starts about 33,800 Python processes (estimate from 20 module traces): about 660 CPU s of bare starts. The repository start hook costs 5 CPU ms for each start. Sent to builds on 11-21 with the proposal: trace the started programs of one gate; Phase 413.
**Depends on:** none

#### Work Order
- **Goal:** one report gives the CPU of one Python process start with the gate environment on tree e7670cf8d7 and on current main, and names the code that added cost. Predicted saving: 0 CPU in this phase (measure). The target is the broad rise of about 1,018 CPU s for each AWS full gate (measured, AWS, Phase 399).
- **Spec:**
  1. In private clones of the two trees, start `python3 -c pass` 200 times with the gate interpreter and the gate environment (sitecustomize from `scripts/lib/python_startup`). Record user plus system CPU for each tree. Fresh TMPDIR on /tmp, bytecode on.
  2. Repeat with `python3 -I -c pass` to give the cost without sitecustomize.
  3. When the tip costs more, use `python3 -X importtime` and a bisect over the commits that changed `scripts/lib/python_startup/` to name the commit and the lines.
  4. Count the Python starts of one full gate from one task_cpu.py trace or from the per-module process counts of module_cost.py on 20 modules. Give the cost times the count.
  5. Proposal to builds: the repair, its CPU saved for each gate, its owner (devtools if 70e6c54344 is the cause). Change no repository file.
- **Files:** none (a report in the session folder).
- **Workers:** 1 writer.
  - impl: all steps; nothing splits.
- **Constraints from prior phases:** Phase 407 found the rise in Python programs with no change in process starts. Runs of test modules need natemerge's go; step 1 to 3 run no test module.
- **Acceptance gate:**
  1. Each number names its tree and its command.
  2. The report says which part of the 407 rise the start cost explains.

### Phase 413 — the repository Python start hook has a measured cost for each part  · status: done (no change; measure)

**As built:** no code change. The hook adds 4.42 CPU ms for each Python start (natedev, main 262952c406, 200 starts). `from typing import cast` at its two call sites is 2.85 ms; the chained hook lookup is 1.08 ms; the rest is under 0.5 ms each. Phase 415 removes the runtime casts. A separate start entry for confirmed checkouts reaches 0.37 ms but can break external hooks, coverage and Mac Python: not planned.
**Depends on:** none

#### Work Order
- **Goal:** one report gives the CPU of each part of `scripts/lib/python_startup/sitecustomize.py` at one Python start, and a repair that brings the hook under 1 CPU ms. Baseline: about 5 CPU ms for each start, about 165 CPU s for each full gate (estimate: 33,800 starts, Phase 412). Predicted saving: 0 CPU in this phase (measure and proposal).
- **Spec:**
  1. Read the hook and each module it imports. Name each part: imports, the fixture file tuple, the dataclass code cache, environment reads, file reads.
  2. Measure each part: 200 starts of `python3 -c pass` with the gate environment, with the full hook and with each part removed in a private clone of main. Use `-X importtime` for the imports. Fresh TMPDIR on /tmp, bytecode on, 2 GiB scope.
  3. Say which parts each Python start needs and which only a test runner or a named program needs.
  4. Proposal: the repair with its CPU for each start after it, its owner, and its risk. Change no repository file. Run no test module.
- **Files:** none (a report in the session folder).
- **Workers:** 1 writer.
  - impl: all steps; nothing splits.
- **Constraints from prior phases:** Phase 412: the start cost is equal on old and new trees; the Nix interpreter keeps its own installed hook under `-I`.
- **Acceptance gate:**
  1. Each number names its command and tree.
  2. The parts add up to the measured hook cost within 0.5 ms.

### Phase 414 — the programs that the tests of one gate start are ranked by CPU  · status: todo
**Depends on:** none

#### Work Order
- **Goal:** one list of each program that the tests of a full gate start, with its starts and its CPU, from one normal gate run. Baseline: about 33,800 Python starts and 660 CPU s of bare starts for each gate (estimate, Phase 412). Predicted saving: 0 CPU in this phase (measure); the list selects the repairs.
- **Spec:**
  1. Step 1, design only, no gate: find a method that reads process exit records from outside the gate processes and does not attach to them (for example kernel process accounting, taskstats exit records, an eBPF exit probe, or the cgroup of each job plus /proc reads). `task_cpu.py` uses ptrace and is not allowed on a merge gate. Say what each method needs (root, a kernel option, a package) and whether natedev has it.
  2. Measure the method's cost on `python3 -c pass` loops and on the Phase 412 empty runner: CPU of the reader, and the change in CPU and wall time of the measured commands. Run no test module.
  3. Give the exact command for a full gate, what it can cost the run, and how it fails safe (the reader stops; the gate continues).
  4. natemerge picks the gate run. bob's condition: no extra run; natemerge decides after it reads the cost.
  5. From the records: a table of program (script or module name), starts, CPU s, and the test modules that start it most. Proposal for the top programs: the test calls the function in the test process, or the program imports only what its command needs. No lazy imports.
- **Files:** none in steps 1 to 4 (a report in the session folder).
- **Workers:** 1 writer.
  - impl: all steps; nothing splits.
- **Constraints from prior phases:** Phase 412: the Python start cost is equal on old and new trees; the rise is in what started programs import. B (update_registration.py imports, owner sdlc) waits for this list.
- **Acceptance gate:**
  1. The reader's measured cost is stated before any gate carries it.
  2. The table's CPU total is compared with the gate record's module CPU.

### Phase 415 — the Python start hook does not import typing  · status: todo
**Depends on:** Phase 355

#### Work Order
- **Goal:** `scripts/lib/python_startup/sitecustomize.py` imports no `typing` at run time, so each Python start in the repository uses about 2.85 CPU ms less. Predicted saving: about 96 CPU s for each full gate (computed: 2.85 ms times 33,800 starts, natedev estimate), and the same cut for each Python start outside the gate.
- **Spec:**
  1. Remove the two `from typing import cast` imports (lines 264 and 283 on main 262952c406) and their `cast` calls. Give the two values their types without a run-time import: an annotated local with an `isinstance` or `callable` narrowing that is already true for the value, or a `TYPE_CHECKING` block at module scope. Add no line-level type ignore unless no typed form exists; then give the cause.
  2. Change no hook behavior and remove no check (condition from bob, 11-21). If the saving needs either, stop and report.
  3. Proof row, failing first: a test starts `python3 -c "import sys; print('typing' in sys.modules)"` with the repository hook and expects `False`. Put it in the existing test module for the hook.
  4. Measure 200 starts before and after with the Phase 413 probe; give CPU ms for each.
- **Files:** `scripts/lib/python_startup/sitecustomize.py`; the hook's existing test module.
- **Workers:** 1 writer.
  - impl: all; nothing splits.
- **Constraints from prior phases:** Phase 355 changes this file: the branch starts at the 355 tip a85f760649 and its hash goes to builds after 355 is on main. Phase 413 holds the probe in /tmp/tmp.g47POH4T8s.
- **Acceptance gate:**
  1. The proof row fails on the base and passes on the fix.
  2. The hook's test module passes; basedpyright zero errors and warnings on changed files.
  3. Measured CPU ms for each start before and after.

### Phase 416 — the gate has no integrate form  · status: todo
**Depends on:** Phase 355

#### Work Order
- **Goal:** The gate code has no integrate entry point. The user approved the removal on 11-21 through bob: "remove integrate form". No check goes away.
- **Spec:**
  1. Remove the old merge entry point of `scripts/production/gate.py` (the Phase 355 name of `merge_gate.py`) and each private helper that only it calls. Ask devtools for its note first; it names the lines.
  2. Remove the classifier at `scripts/production/build_steps.py:297` (line on main before 355) and the three text keys `names.integrate`, `about.integrate` and `changes.integrate` in `config/build_steps.conf`. The user go covers these key deletions.
  3. Remove the merge cases in the `test_gate*.py` modules, the case at `scripts/build_queue/test_memory_feed.py:647` and the case at `memory_page_proof.mjs:2709`.
  4. Keep `--promote-only`, test all and the recovery of existing records. A saved record with the integrate step stays readable.
  5. Correct `docs/as-built/merge-gate-and-test-speed.md:313` in the same commit.
  6. Before the removal, prove with a search that no caller starts the integrate form: scripts, commands, config, units and timers.
- **Files:** `scripts/production/gate.py`; `scripts/production/build_steps.py`; `config/build_steps.conf`; the `scripts/production/test_gate*.py` modules; `scripts/build_queue/test_memory_feed.py`; `memory_page_proof.mjs`; `docs/as-built/merge-gate-and-test-speed.md`.
- **Workers:** 1 writer + 1 tester.
  - impl: `gate.py`, `build_steps.py`, `config/build_steps.conf`, the as-built doc.
  - test: the test modules and `memory_page_proof.mjs`.
- **Constraints from prior phases:** Phase 355 renames the gate files; the branch starts at main after 355 is there. Phase 355 keeps `merge_gate.py` as a compatibility entry point; this phase does not remove it.
- **Acceptance gate:**
  1. The search of item 6 finds no caller.
  2. Each changed test module passes; a test proves a saved integrate record stays readable.
  3. basedpyright zero errors and warnings on changed files.
  4. A deletion change runs the full gate at the merge.

### Phase 417 — a failed merge batch has one path: each row goes back to its requester  · status: todo
**Depends on:** Phase 355

#### Work Order
- **Goal:** `merge_checkpoint.py` has one failure path. User rule, 11-21 through bob: "if a merge fails - it gets sent back to the requester to fix and the next thing in line should proceed". This phase runs before Phase 416.
- **Spec:**
  1. The first stop ends the batch: a red full gate, or a merge conflict with main. No probe, no retry and no single-match case.
  2. Each row goes back to its requester with its report: the red steps, and whether the tests of that row include a red step.
  3. The next item in the line proceeds.
  4. Remove `bisect_red_batch`, the row-matching block in `retest`, the retry loop in `run_recorded_batch`, and `later_item_stop` if no caller remains.
  5. In `test_merge_checkpoint_batch.py` remove the bisect tests and the retry tests. Keep one test: a red batch stops every row and reports each one.
  6. Remove the `BATCH_GATE_TRIES` key and its readers. The user rule covers this deletion.
  7. Correct each doc that names the removed paths in the same commit.
- **Files:** `scripts/production/merge_checkpoint.py`; `scripts/production/test_merge_checkpoint_batch.py`; the config file that holds `BATCH_GATE_TRIES`; the docs that name the removed paths.
- **Workers:** 1 writer.
  - impl: all; the code and its one test module do not split.
- **Constraints from prior phases:** Phase 355 changes `merge_checkpoint.py`; the branch starts at the 355 preparation tip and its hash goes to builds after 355 is on main. Phase 409 loses its bisect part and Phase 411 merges on top of this phase.
- **Acceptance gate:**
  1. A search finds no `bisect_red_batch`, no `BATCH_GATE_TRIES` and no retry loop.
  2. The kept test fails without the report of each row and passes with it.
  3. Each `test_merge_checkpoint*.py` module passes; basedpyright zero errors and warnings on changed files.
  4. A deletion change runs the full gate at the merge.

### Phase 418 — each merge batch runs in its own temporary worktree  · status: todo
**Depends on:** Phase 417
**Blocked by:** tonight's Mac work (builds, 2026-11-21 17:46)

#### Work Order
- **Goal:** a merge batch rollback never changes operator state. builds approved this on 2026-11-21 at 17:46 after a design check (`/tmp/claude/astra/20261121T223908Z-990209/review.md`) listed operator state that the rollback in the shared checkout can change, on main as well.
- **Spec:**
  1. `merge_checkpoint.py` runs each batch (merges, gate, publication, rollback) in a new temporary worktree at the batch start tip. Make that worktree through the build-folder maker of resources item 143. No operator works in it.
  2. Rollback and conflict cleanup act only in that worktree. Remove the ownership checks that only protect a shared checkout (open-operation checks, index and HEAD checks for operator edits) when the temporary worktree makes them dead.
  3. Records stay outside the worktree, where they are now. Recovery of an interrupted batch reads them and makes a new temporary worktree; it never reuses a leftover one.
  4. The worktree goes away at batch end on every path (pass, red, conflict, crash recovery). A leftover from a crash is removed at the next batch start.
  5. Cost per batch, measured on natedev 2026-11-21: about 0.8 s wall and 0.2 CPU s to add and remove one worktree; about 64 MB disk while the batch runs.
- **Files:** `scripts/production/merge_checkpoint.py`; `scripts/production/checkpoint_gate.py`; `scripts/production/test_merge_checkpoint*.py`; the item 143 build-folder maker (caller only); `docs/as-built/merge-gate-and-test-speed.md`.
- **Workers:** 1 writer.
  - impl: all; the batch code and its tests do not split.
- **Constraints from prior phases:** Phase 417 gives one shared stop path with a fixed cleanup order and ownership checks for the shared checkout; this phase replaces those checks where the temporary worktree makes them dead.
- **Acceptance gate:**
  1. A test opens an operator merge, a staged file and a paused rebase in the main checkout during a failing batch; all stay unchanged.
  2. No temporary worktree remains after pass, red, conflict and recovery tests.
  3. Each `test_merge_checkpoint*.py` module passes; basedpyright zero errors and warnings on changed files.
  4. The full gate runs at the merge.

### Phase 419 — a killed page test leaves no browser behind  · status: todo
**Depends on:** none

#### Work Order
- **Goal:** when a page test process dies without its cleanup (time limit, SIGKILL, a stopped shell), its headless Firefox and Chrome stop too. On 2026-11-21 nate found a headless Firefox from `scripts.review_links.test_list_page` alive for two days (13 processes, 0.42 GiB) after its test shell ended.
- **Spec:**
  1. `test_list_page.py:121-127` and `:250-253` start Chrome and Firefox with `start_new_session=True`. Cleanup (`_stop_browser`, class cleanups) runs only when the test process lives to run it, and a group kill of the test does not reach the new session.
  2. Make each browser stop when the test process that started it dies, on every exit path, with one shared helper for all page tests that start a browser (`test_list_page.py`, `test_review_page.py`, `test_accounts_page.py`, `test_serve.py`; check each). Add no signal handler, mask or disposition in the test process (builds rule, 2026-11-15).
  3. Keep `_stop_browser` group stops for the normal path.
- **Files:** `scripts/review_links/test_list_page.py`; the other page test modules that start a browser; one shared test helper module in `scripts/review_links/`.
- **Workers:** 1 writer.
  - impl: all; one shared helper and its callers.
- **Constraints from prior phases:** none.
- **Acceptance gate:**
  1. A test starts the helper's browser stand-in from a child process, SIGKILLs that child, and proves the browser process ends within 10 s.
  2. Each changed page test module passes; basedpyright zero errors and warnings on changed files.

### Phase 420 — the drain never counts a test fixture as a waiting gate  · status: todo
**Depends on:** none

#### Work Order
- **Goal:** a hold drain stops only real waiting gates. On 2026-11-21 at 18:25 the gate rename deploy failed at step 4: it read a `verify.sh` test fixture (`/dev/shm/tr-*/.../verify.sh test sample`, inside `unshare --pid` under `module_run.py`) as a waiting gate, and the fixture did not exit in 120 s. builds approved this fix at 18:28.
- **Spec:**
  1. `hold_enforcer.is_waiting_gate` (`scripts/build_hold/hold_enforcer.py:715-732`) counts every process whose script is named `verify.sh`.
  2. Skip a process whose pid namespace (`/proc/<pid>/ns/pid`) differs from the enforcer's own. Test fixtures run under `unshare --pid`; real gates never do. Read the namespace with the other audit fields in `build_hold.read_audit_processes`, so classification stays a pure function of the audit record.
  3. An unreadable namespace keeps today's rule (count the process), so a real gate is never missed.
- **Files:** `scripts/build_hold/hold_enforcer.py`; `scripts/build_hold/build_hold.py` (audit record); `scripts/build_hold/test_hold_enforcer.py`.
- **Workers:** 1 writer.
  - impl: all.
- **Constraints from prior phases:** none.
- **Acceptance gate:**
  1. A table row: a `verify.sh` process in another pid namespace is not a waiting gate; the same process in the enforcer's namespace is; an unreadable namespace is.
  2. `test_hold_enforcer` and the `build_hold` tests pass; basedpyright zero errors and warnings on changed files.

### Phase 421 — a gate reads its time limit from a small recent-times file  · status: todo
**Depends on:** none

#### Work Order
- **Goal:** a gate never updates, rebuilds or locks the buildlog index to get its time limit. It reads a small recent-times file. On 2026-11-21 a merge gate spent more than 60 min with no tests in `index.update()` after the gate rename deploy rewrote the history files. nate approved (List item 190, via bob and builds, 19:31): "1. a small file for the time limit - yes - of course - duh".
- **Spec:**
  1. `gate.read_time_limit` (`scripts/production/gate.py:1028`) calls `index.update()` and then `gate_record.history_time_limit` (`scripts/test_times/gate_record.py:425`), which reads only the newest `TOP_RUNS` (5) passing walls: full runs keyed by vCPU count and job count, and per-module walls keyed by vCPU count and module.
  2. One small file keeps exactly those values: for each full-run key and each module key, the newest `TOP_RUNS` passing walls with their start times. Each passing gate record adds its walls at the place where natedev writes or imports a gate record (local `write_gate_run`, the cloud gate result, the Mac gate result; find every writer). One shared update function; atomic replace under a lock.
  3. `read_time_limit` reads only this file. With no entry for the key, the result is `NoGateTimeLimit` (today's no-history rule). The selection rules stay the same (status, aborted, selection, step, modules_run, restart, attempt).
  4. One seed command fills the file once from the committed index (read-only SQL, no `index.update()`).
  5. No config key changes; no check goes away (builds limit). If one must, stop and tell builds.
- **Files:** `scripts/test_times/gate_record.py`; `scripts/production/gate.py`; one new module in `scripts/test_times/` for the file; the cloud and Mac record importers; their tests.
- **Workers:** 1 writer.
  - impl: all; one shared file reader and writer.
- **Constraints from prior phases:** Phase 409 (in the merge line) adds a per-module rerun limit that shares the per-module median read; when 409 is on main, merge main and move that read to the file too.
- **Acceptance gate:**
  1. Table rows: the full-run limit and the per-module limit from the file equal the index result for the same records; no entry gives `NoGateTimeLimit`; a failed, aborted or restarted run adds nothing; the file keeps only `TOP_RUNS` walls per key.
  2. A test proves `read_time_limit` opens no index file and takes no index lock.
  3. The changed test modules pass; basedpyright zero errors and warnings on changed files.

### Phase 422 — affected-test choice reads a small latest-reads file  · status: todo
**Depends on:** Phase 421

#### Work Order
- **Goal:** a gate run record keeps times, results and memory peaks and no file-read lists. The config reads, folder scans and content reads of the newest full gate that has each go into one small latest-reads file, and `affected_modules` reads that file, not the index. nate approved (List item 190 point 2, via bob, 19:37): "you can ask for the affected test data source and read to change".
- **Spec:**
  1. `affected_modules.latest_module_reads` (`scripts/production/affected_modules.py:723`) calls `index.update()` and selects, for each of the three fields, the newest full non-aborted gate that has it. Keep that selection rule exactly.
  2. Each gate that would write the lists writes them to the latest-reads file instead (atomic replace under a lock), per field, only for a full non-aborted run. The gate run record no longer holds `config_reads`, `folder_scans` or `content_reads`.
  3. `latest_module_reads` reads only the file. A missing or corrupt file gives the same result as today's empty history.
  4. The method of the affected-test choice does not change; no check goes away. If one must, stop and tell bob.
- **Files:** `scripts/production/affected_modules.py`; the gate record writers (`scripts/test_times/gate_record.py`, the cloud and Mac importers); the Phase 421 module folder; their tests.
- **Workers:** 1 writer.
  - impl: all; the same writer as Phase 421, on top of its branch.
- **Constraints from prior phases:** Phase 421 adds the recent-times file and its writers; use the same writer hook.
- **Acceptance gate:**
  1. Table rows: `latest_module_reads` from the file equals the index result for the same records; a non-full or aborted run does not replace the file; a new gate record has no read lists.
  2. A test proves `latest_module_reads` opens no index file and takes no index lock.
  3. The changed test modules pass; basedpyright zero errors and warnings on changed files.

### Phase 423 — old gate records lose their read lists once  · status: todo
**Depends on:** Phase 421, Phase 422

#### Work Order
- **Goal:** the history files shrink from 9.5 GiB to a small part of that. Each gate record holds about 10 MB of read lists (content_reads 7.45 MB, folder_scans 2.16 MB in one natedev record). nate approved (via bob and builds, 19:31): "3. yes". Phase 422 moves the newest lists to the latest-reads file first, so this is the only rewrite.
- **Spec:**
  1. A one-time deploy step first seeds the Phase 422 latest-reads file from the newest full, non-aborted gate record that has each field (the rows `affected_modules.py:723` selects), then rewrites the month files without `content_reads`, `folder_scans` and `config_reads` in any gate record.
  2. The same step updates the index rows in place and writes the new file marks for each rewritten file, so the index reads no file again.
  3. It runs under a drain hold with the buildlog timer stopped, like the gate rename deploy, at a time natemerge picks after the Part 1 gate. No reader changes.
  - **Files:** one deploy module under `scripts/production/`; its tests.
- **Workers:** 1 writer.
  - impl: all.
- **Constraints from prior phases:** Phase 421: gates no longer read the index for the time limit.
- **Acceptance gate:**
  1. Table rows on a fixture history: lists removed from every record; the latest-reads file holds the newest lists; `latest_module_reads` returns the same result before and after; the index reads no file again after the step.
  2. A dry run on natedev states the bytes before and after.

### Phase 424 — a test run folder leaves room for a 108-byte socket path  · status: todo
**Depends on:** none

#### Work Order
- **Goal:** every test module that a gate or a targeted run starts gets a folder path short enough that its Unix sockets fit the 108-byte AF_UNIX limit. builds 20:12 (from sdlc): admin-console's targeted run (repofix, 18:47-20:03) exited 1 with about 340 errors: "OSError: AF_UNIX path too long" (test_add_unit*, test_crate_run, test_unit_move, test_disk) and Chrome "Socket path too long" (test_list_page, 88 errors in each part). The Mac part fell back to natedev and met the same errors.
- **Spec:**
  1. Find the cause first. The gate folder is `<tmpfs>/<tool>-N` (`gate_temporary`, `scripts/production/gate.py:888`; tool `targeted-test` or `merge-gate`, `gate.py:1576`). Each module gets `build_folders.tool_folder(f"{suite}-{path.stem}", temporary)` (`gate.py:1734`). `testing_folders.test_run_folder` (`scripts/lib/testing_folders.py:20`) adds `test-runs/test-run-<id>`. Tests then add tempfile folders and socket names. Print the real deepest socket path of each failing module and its byte count. Say why the targeted form fails and the merge gate does not (if it does not), and whether a recent change (for example `7cd713d141` build folders track A) made a name longer.
  2. Repair where the path is made, not in each test, and the same for the gate and targeted forms. Prefer a short module folder name (for example a short index or hash in place of `<suite>-<stem>`) and a short test-run name. Keep each folder unique and keep the owner and lease files the sweepers read.
  3. If a folder name changes, change every reader of that name in the same commit: sweepers, recovery, retained-folder reports, docs. grep for each old prefix.
  4. One test asserts the budget: the deepest folder that the gate and `test_run_folder` make, plus a fixed socket-name budget, fits in 108 bytes. Take the budget from the longest socket name in the failing tests.
  5. No config key changes; no check goes away.
- **Files:** `scripts/production/gate.py`; `scripts/lib/testing_folders.py`; `scripts/lib/build_folders.py` (only if the name is made there); their tests; the as-built doc that names the folder layout.
- **Workers:** 1 writer.
- **Acceptance gate:** the failing modules named above pass in one tests-only gate; the budget test passes; basedpyright zero errors and warnings on changed files; one blind review.

### Phase 425 — a failed recovery of another checkout's gate record does not stop every gate  · status: todo
**Depends on:** none

#### Work Order
- **Goal:** one bad dead-gate record no longer refuses every gate on the host. builds approved at 21:20: "A failed recovery of another repo's record reports and continues; only the gate's own repo refuses."
- **Spec:**
  1. Every gate start recovers the records of all checkouts (`recover_gate_runs`, `scripts/production/gate.py:774-783`, called at `:839` and `:1437`). A recovery that raises `GateRefused` (`:666`, `:702`, `:741`, `:799`) now stops every gate on the host.
  2. For a record in another checkout's directory, catch `GateRefused`, print one stderr line with the record path and reason, keep the record, and continue with the next record and directory.
  3. For the gate's own checkout directory, keep the current refusal.
  4. Change no other recovery step; no config key changes; no check goes away.
- **Files:** `scripts/production/gate.py`; `scripts/production/test_gate.py` (or the test module that holds the recovery tests); `docs/as-built/merge-gate-and-test-speed.md` if it describes recovery.
- **Workers:** 1 writer.
- **Acceptance gate:** fixtures: a refusing record in another checkout is reported and kept while the gate runs; a refusing record in the own checkout still refuses; the recovery test module passes in one tests-only gate; basedpyright zero on changed files; one blind review.

### Phase 426 — an ended Mac gate attempt with a receipt clears its journal  · status: todo
**Depends on:** none
**Blocked by:** nate's 22:35 session restart (builds 22:13: start after the restart)

#### Work Order
- **Goal:** a Mac gate attempt that ended with a receipt and a kept checkout no longer leaves process groups in its journal. builds 22:13 (anomaly from sdlc and devtools): the nosiggate run that stopped on EPERM at about 21:45 EST still lists four process groups; the gate folder is gone.
- **Spec:**
  1. `scripts/mac_test/mac_gate.py:234` reads `cleanup_paths = cloud_gate.read(receipt)` and calls `stop_guest(folder, deadline)` only when the receipt is missing (`FileNotFoundError`). An attempt with a receipt never clears its journal rows.
  2. For an ended attempt with a receipt, also check each journal process group (pid plus start), stop the live ones as `stop_guest` does, then remove the journal row. Reuse `stop_guest`'s group step; copy no code.
  3. The same when the gate folder is already absent (`folder.exists()` false at `:227`): the journal rows of that attempt are still cleared.
  4. No config key changes; no check goes away.
- **Files:** `scripts/mac_test/mac_gate.py`; its test module; the as-built doc that describes Mac gate cleanup.
- **Workers:** 1 writer.
- **Acceptance gate:** fixtures: an attempt with a receipt and a kept checkout clears its journal and stops a live recorded group; an attempt whose folder is gone clears its journal; the Mac gate test module passes in one tests-only gate (Mac part through the queue); basedpyright zero on changed files; one blind review.

### Phase 427 — a Mac run under a block it does not hold waits for the block end  · status: todo
**Depends on:** none

#### Work Order
- **Goal:** `scripts/mac_test/mac_queue_run.py --kind run` honors the mac_test block. builds 23:02: two devtools Mac runs (idlepanes2 at 22:57, verifytoken at 23:00) started under the builds block for item 191 run 2; the file has no block read.
- **Spec:**
  1. Before a `--kind run` starts its remote command, read the Mac block with the reader that `mac_test.py` and the gate use (`read_block` / `blocking_claim_block` / `free_now` in `scripts/mac_test/mac_test.py`). Copy no block logic.
  2. A run under a block that its session does not hold follows the behavior `mac_test.py` claims and the gate already use for the same case: wait for the block end with one wait line, or end with one clear refusal that names the holder and reason. Pick the one those callers use; state the choice in the commit.
  3. A run by the block holder starts as before. Other kinds (`shot`, `install`, `probe`) keep their current behavior unless the same callers treat them the same way.
  4. No config key changes.
- **Files:** `scripts/mac_test/mac_queue_run.py`; its test module; the as-built doc that describes Mac queue runs.
- **Workers:** 1 writer.
- **Acceptance gate:** fixtures: a run under another holder's block waits (or refuses) with one line and starts no remote command; a holder's run starts; no block starts as before; the changed test modules pass in one tests-only gate (Mac part through the queue); basedpyright zero on changed files; one blind review.

### Phase 428 — a stopped test run leaves no active-runs record  · status: todo
**Depends on:** Phase 425

#### Work Order
- **Goal:** a test run stopped by SIGTERM closes or removes its record in `~/.local/state/gate/active-runs/<repo>/`, and a later run drops a record whose pid is dead. builds 23:10: three records from stopped efficiency phase 46 test runs (pids 3022152 about 21:18, 3306214 at 22:56, 646641 at 23:10 with finished false), all exit 143; a dead record makes the next gate fail. builds 23:18: a fourth, pid 3400386 (resources 143v run, stopped 23:13, repo /build/claude/resources/3) has finished true; the fix also covers a finished record that stays behind (`GateRecovery.finish` keeps a record that names folders).
- **Spec:**
  1. Find why a SIGTERM stop skips the record close (`scripts/production/gate.py`; `GateRecovery.finish` :748-754 keeps a record that names folders; `recover_gate_directory` :786-819 skips finished records at :794 and checks pid plus start at :796-800). Name the path with file:line, and why a dead record fails the next gate after 425.
  2. On SIGTERM, the test run closes or removes its record through the existing finish path. Copy no code.
  3. A later run drops a record whose pid plus start is dead, and recovers its named folders, as `recover_gate_directory` does for other records. A failure stays isolated to that record (425).
  4. No config key changes; no check goes away.
- **Files:** `scripts/production/gate.py`; its test module; the as-built doc that describes gate recovery.
- **Workers:** 1 writer.
- **Acceptance gate:** fixtures: a SIGTERM stop leaves no open record; a dead-pid record with finished false is dropped by the next run and that run passes; the changed test modules pass in one tests-only gate; basedpyright zero on changed files; one blind review.

### Phase 429 — cleanup releases a legacy Mac claim only for a verified repository  · status: todo
**Depends on:** none

#### Work Order
- **Goal:** builds 00:39 (queue unit item 28, macclaim-legacy, moved to natest): a cleanup releases a legacy `run.json` Mac claim only when the claim names a verified repository. A relative worktree path with no repository metadata matches nothing.
- **Spec:**
  1. `scripts/mac_test/claim_state.py:444-450` (`target_claim_scope`) gives a worktree with no repository name or common dir the whole Mac scope; `:490` (`read_claims`) gives a relative legacy worktree the whole Mac scope. Find each cleanup caller that releases a claim by scope match, with file:line.
  2. A cleanup releases a legacy `run.json` claim only when its repository is verified (repository name and key from the claim's absolute worktree). A legacy claim with a relative path and no repository metadata matches no cleanup; it stays until its owner releases it or the existing expiry ends it.
  3. Claim reading and admission keep their current scope (a legacy claim with no repository still blocks the whole Mac). Copy no scope logic.
  4. No config key changes; no check goes away.
- **Files:** `scripts/mac_test/claim_state.py`; its test module. verify owns `claim_state.py` and reviews the change.
- **Workers:** 1 writer.
- **Acceptance gate:** fixtures: a cleanup for a verified repository releases its own legacy claim; a cleanup never releases a relative-path legacy claim with no repository metadata; claim reading keeps the whole-Mac scope for that claim; the changed test module passes in one tests-only gate after the builds go; basedpyright zero on changed files; review by verify.

### Phase 430 — the ledger watch test checks the day edges, not every 5 seconds  · status: todo
**Depends on:** none

#### Work Order
- **Goal:** `aws_target/test_ledger.py` drops from 56.1 median CPU s (rank 2, `show.py top` 00:39) to under 5. One test is the whole cost: `SettledChargeWatchTests.test_wider_watch_keeps_the_daily_query_interval_after_nine_days_of_reads` (57.3 s), which calls `refresh_charges` 51,840 times (`range(5, 3 * day + 1, 5)`, test_ledger.py:2021).
- **Spec:**
  1. Keep the same claim with the same assertions: after the watch widens to 45 days, the fetch runs once each day and never between. Check the times that can break that claim: the first step, 5 s before and at each day edge, and the last step, for each of the three days. Keep the final `call_args_list` assertion unchanged.
  2. Change no product code. Check that no other test in the module repeats a dense time sweep; report any with its cost.
  3. No config key changes; no assertion goes away.
- **Files:** `scripts/aws_target/test_ledger.py` (aws owns it; aws gets the diff before the hash).
- **Workers:** 1 writer.
- **Acceptance gate:** the module passes in one tests-only gate after the builds go; module CPU from the gate record under 5 s; basedpyright zero on the changed file; one blind review.

### Phase 431 — no test or product code makes a folder in /tmp outside the model  · status: todo
**Depends on:** none

#### Work Order
- **Goal:** nate 00:49 via builds 00:53: "there is NOTHING that should live outside the model". No file under `scripts/` makes a folder or file at a literal `/tmp` path (`dir="/tmp"`, `tempfile` with `/tmp`, `Path("/tmp...")`, `"/tmp/..."`). A grep of origin/main 0e4ea6e563 finds 91 candidate files (sdlc listed 14, for example `production/gate_test_support.py:344`, `lib/test_socket_test_support.py`, `agents/test_worker_lifecycle.py`, `production/test_stall_watch.py`, `message/send.py`).
- **Spec:**
  1. List each real case with file:line; drop matches that only read or name `/tmp` (docs, refusals, assertions about other paths). Leave cases that devtools moves on its own branch (ask builds for that list).
  2. Move each test folder to the test folder model (`testing_folders` / `build_folders.tool_folder`); move product code to the model root it belongs to. One shared helper; copy no code.
  3. A test asserts that no file under `scripts/` (except an allowlist with a reason per line) holds a literal `/tmp` folder path.
  4. No config key changes; no check goes away.
- **Files:** the listed files; `scripts/lib/testing_folders.py` only if a helper is missing.
- **Workers:** 2 writers by folder group if the list is long.
- **Acceptance gate:** the changed modules pass in tests-only gates after the builds go; the guard test passes; basedpyright zero on changed files; one blind review.

### Phase 432 — the disk snapshot test reads no live sccache folder  · status: todo
**Depends on:** none

#### Work Order
- **Goal:** builds 01:17 (sdlc; devtools saw it at 01:00 and 01:14): `buildlog/test_disk.py:730` `test_snapshot_reads_the_file_it_will_replace` fails at random. `disk.py:407-409` adds the live `SCCACHE_DIR` as a scanned folder; the sort at `disk.py:213` can put its zero-growth row first, so `assertGreater(outside_build_caches[0]["growth_bytes"], 0)` sees 0.
- **Spec:**
  1. The test sets its own fixture: `SCCACHE_DIR` is unset, or points at a fixture folder, for every snapshot in the test. Prefer the fixture class, so no test in the module reads the live cache.
  2. Keep every assertion. Change no product code.
  3. No config key changes.
  4. Added builds 01:20 (sdlc): the reservation test in `production/test_gate_suites_admission.py` reads the live `/proc/pressure/memory`; it failed once and passed on rerun. Give it a fixture pressure read.
  5. Note: the `setUp` pop of `SCCACHE_DIR` is on main since 9b98021e11 (11-21 21:55); a branch without it still fails.
- **Files:** `scripts/buildlog/test_disk.py`; `scripts/production/test_gate_suites_admission.py`.
- **Workers:** 1 writer.
- **Acceptance gate:** the test passes with a live `SCCACHE_DIR` set; the reservation test reads no live pressure file; the module passes in one tests-only gate after the builds go; basedpyright zero on the changed file; one blind review.

### Phase 433 — two test fixtures name hanallc/hana  · status: todo
**Depends on:** none (builds 01:24: start after 431 and 432)

#### Work Order
- **Goal:** builds 01:24 (sdlc admin-console review): `config/hana_aws.conf:8` names `hanallc/hana`, but `scripts/aws_target/fixtures/cloud_gate.conf:30` and `scripts/mac_test/test_hana_offload.py:69` still name `natepiano/hana`. Both fixtures name `hanallc/hana`.
- **Spec:** change the two fixture values; keep every assertion; no product code or config key changes.
- **Files:** `scripts/aws_target/fixtures/cloud_gate.conf`; `scripts/mac_test/test_hana_offload.py`.
- **Workers:** 1 writer.
- **Acceptance gate:** the modules that read the two fixtures pass in tests-only gates after the builds go; one blind review.

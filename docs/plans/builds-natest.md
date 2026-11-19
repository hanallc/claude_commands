

### Phase 381 — fast_track builds the selected tree once  · status: done (no change)

**As built:** no code change. Using the first private index tree removed 2 of 13 Git starts per selection, but two reviews found that this tree differs from `HEAD` plus `apply --cached` of the patch: selected submodules, `apply.whitespace=fix`, `diff.context` (the recovery digest changes) and diff drivers. Recovery then fails or removes a local edit. The `apply --cached` step is the check that keeps the recovery tree equal to the published patch. Branch natest-selectpatch381 keeps the attempt.

### Phase 382 — the offload cleanup test halves its AWS CPU  · status: todo
**Depends on:** none

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

### Phase 391 — the gate wakes on child exit and reads job memory from its cgroup  · status: todo
**Depends on:** Phase 388 (merge_gate.py job environment); builds orders the start (merge_gate.py is busy)

#### Work Order
- **Goal:** merge_gate.py stops timed polls of its jobs: it wakes on child exit (pidfd) and reads each job's memory from the job cgroup, so the gate supervisor uses less CPU on every gate run (proposal from 385: child runners were 45.3% of the base trace CPU; builds accepted 11-19).
- **Spec:**
  1. Step 1, design only: measure nothing; read merge_gate.py and name each timed poll, each per-process memory sample, and the exact lines and files to change. Send builds the file list before any code (builds orders merge_gate.py work).
  2. Wake on child exit: one select.poll (or selectors) loop over the job pidfds plus the deadline; no fixed sleep.
  3. Memory: read memory.current / memory.peak of the job scope cgroup; no walk over /proc per process.
  4. Keep the gate's stop, timeout and memory-stop order unchanged; keep 383 recovery records.
  5. Failing first: a test row shows the gate makes no wake while jobs run and nothing exits (count wakes), and one shows memory comes from the cgroup file.
  6. Tests read no /tune value; no signal handlers.
- **Files:** `scripts/production/merge_gate.py` (or gate.py if 355 has landed); one gate test module for the rows.
- **Workers:** 1 writer.
  - impl: design note, then the code and rows.
- **Constraints from prior phases:** 383 recovery and 388 allow list change merge_gate.py; 355 renames it to gate.py. User test hold: no test runs until natemerge ends it.
- **Acceptance gate:**
  1. Gate supervisor CPU on a paired local gate run drops, with the number.
  2. The new rows fail on the base and pass on the fix.
  3. 20 repeated runs of the gate test modules pass, TMPDIR=/tmp, 2 GB scope.
  4. basedpyright zero errors and warnings on changed files.
  5. Full natedev gate passes (reds that also occur on base do not count).

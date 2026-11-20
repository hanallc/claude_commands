

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
**Depends on:** Phase 392 (merge_gate.py job start and stop); 383 (bb1) lands first. Queue 44df is dropped (bob, List item 140): job memory comes from each module's own scope, with no gate scope.

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

### Phase 392 — bubble-wrapped tests on natedev for each start path  · status: todo
**Depends on:** Phase 389 (plan 6994708e9 and bob's review); 383 lands first

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
- **Constraints from prior phases:** 383 lands first; 355 renames merge_gate.py to gate.py (merge main before checkpoint). 388 lands no code (folded here). User test hold: write and review now; run tests only after natemerge releases units. NixOS entry for bubblewrap is asked of natedev; until then the config names the store path.
- **Acceptance gate:**
  1. The proof module passes behind the guard and fails with the guard removed (refusal row).
  2. One full natedev gate inside Bubblewrap; a failing module is fixed or reported with its reason; record the gate time with and without the guard and the cold bytecode cost (compileall pass if any loss).
  3. 20 repeated runs of the changed modules pass, TMPDIR=/tmp, 2 GB scope.
  4. basedpyright zero errors and warnings on changed files.

### Phase 393 — TERM stops a gate in a known time (no cleanup signal mask)  · status: todo

**Depends on:** none. Ordered after the red main repair and the guard repair (builds); does not move queue 44dg.

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



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



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

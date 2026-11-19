

### Phase 381 — fast_track builds the selected tree once  · status: todo
**Depends on:** Phase 373

#### Work Order

**Goal:** each fast-track selection builds its worktree tree in one private index, so every fast-track push and `production/test_fast_track.py` start fewer Git processes.

**Spec:**
- Phase 373 named the cause. `patch_for()` (`fast_track.py:211`) reads HEAD into a private index, adds the paths and diffs it. `select_patch()` (`fast_track.py:490`) then makes a second private index: `read-tree HEAD`, `apply --cached` of that patch, `write-tree`. The first index already holds that tree.
1. `patch_for()` returns the patch and the tree of its private index (`write-tree` in that index). An empty selection returns HEAD's tree.
2. `select_patch()` uses that tree and drops the second index, its `read-tree`, `apply --cached` and `write-tree`.
3. Both reads use the same resolved HEAD commit, so a HEAD change between them still refuses as today.
4. Look for other repeated Git work on the selection path, such as `ls-tree -r HEAD` per call; cut it where the result is the same.
- The selected tree, the patch, the recovery refs and every refusal stay the same. One test row proves the tree equals the old two-index result for an added, a changed, a deleted and an untracked path.
- Every assertion remains. Tests read no /tune-changeable value. No lazy imports, no copies, no signal handlers.
- Phase 376 (natest-reviewlauncher376, not on main) also changes `fast_track.py`, in the `--pending` snapshot only. Whichever merges second resolves.

**Files:** scripts/production/fast_track.py; scripts/production/test_fast_track.py.

**Workers:** 1 writer. One function pair and its tests.
- impl: fast_track.py and its test rows.

**Acceptance gate:** Git starts per selection fall (count them before and after); an AWS pair (3 rounds) of `production/test_fast_track.py` shows less CPU; the module passes 20 times under load in a 2 GB scope; the full suite is green.

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

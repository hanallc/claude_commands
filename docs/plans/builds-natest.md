

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



### Phase 380 — the assign launcher test halves its AWS CPU  · status: todo
**Depends on:** none

#### Work Order

**Goal:** `direct/test_assign_launcher.py` uses half its AWS gate CPU (31.0 s median, 33.4 s wall, 60 tests on 2026-11-19).

**Spec:**
- AWS rank 19 on 2026-11-19 01:46 EST. Ranks above it have phases, are verify.sh modules for phase 375, touch copies.py work in phases 371-372, or have a floor result (`style/test_crate_run.py`, `style/test_bulk_run.py`). Slowest kept test: `AssignLauncherTests.test_only_a_live_claude_or_codex_ancestor_permits_assignment` 2.9 s.
- An earlier phase cut the four assign launcher files by 37% and named the cost left: the process tests in this module and the recorder and resolver children (58.6 CPU s in that command profile).
1. Measure with `module_cost.py --tests 20 --commands 20` like the gate: fresh TMPDIR on /tmp, bytecode on. Name the top commands and fixtures and their CPU share.
2. Cut the cost at its cause: repeated setup, Python starts or polling. Use `FirstDispatchFixture` where a test needs only a restored first dispatch. Keep a real launcher process where a test proves a process boundary.
3. Name any cause in `assign.sh`, the recorder or the resolver; fix it there when it also costs production runs.
- Every assertion remains. Tests read no /tune-changeable value. No lazy imports, no copies, no signal handlers.

**Files:** scripts/direct/test_assign_launcher.py; scripts/direct/assign_launcher_test_support.py; the launcher production code only when the cost lives there.

**Workers:** 1 writer. One module and its subject.
- impl: the test module, its support and any production cause.

**Acceptance gate:** an AWS pair (3 rounds) shows the median CPU at half or less, or a floor result with its cause; the module and its three sibling files pass 20 times under load in a 2 GB scope; the full suite is green.

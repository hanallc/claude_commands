# Build folders

Status: implementation plan. Phase bf1 has landed. The devtools part of Phase 2 (bf2d, bf2p, bf9r) is merged into sdlc.
The build disk is in: `/build` (3.6 TiB) has its file system since 2026-11-14 18:43 EST.
Next: measure the slot values again on `/build` and send them to the user. Then the queue part of Phase 2 (rescue), then Phase 3.
The user approved D1–D9 on 2026-11-12 at 12:45 EST.
This plan has nine phases and an estimate of **64 worker hours**.

## Cut

1. Remove the separate measurement feature and phase. The user approved the slot values on 2026-11-12.
2. Remove the fixture lease API and four separate fixture migration phases. Tests call the maker through one small private-config helper.
3. Merge queue attribution into slots. Merge gate migrations and assign other callers to their existing owners.
4. Remove mandatory human-link trees, extra lease states, persistent deletion records, and a separate rescue manifest format.
5. Remove the long search appendix and repeated test matrices. Keep one maker inventory and focused acceptance gates.

## Approved contract

1. **D1 — One maker.** `scripts/lib/build_folders.py` and `config/build_folders.conf` make every build and test folder.
   Human names have the form `<showrunner>-<unit>/<work>-<purpose>`.
   The work is a phase ID or a short name from the maker.
   Never infer ownership from a path.
2. **D2 — Repository slots.** Every repository task uses a slot pool for its machine and repository.
   The pool grows on demand to a configured maximum (the user, 2026-11-15); a run takes the lowest free number.
   Each slot has stable source and Cargo target paths.
   Taking a slot resets its source and retains its target.
   Convert the Mac, AWS, and natedev gate caches into these slots.
3. **D3 — Visible leases.** Each lease records the showrunner, unit, and purpose.
   The build queue page shows those fields for every occupied slot.
4. **D4 — Size limits.** Each slot has a configured size limit.
   At acquisition, remove the target when the slot exceeds that limit.
   Then run one cold build.
5. **D5 — Process authority.** The working process holds a file lock beside its folder or slot.
   A session never owns a folder.
   Without a held lock, a slot is free and a one-off folder is eligible for removal.
   One-off folders contain temporary files, never repository work.
6. **D6 — Rescue before deletion.** Save all Git work under `refs/rescue/<name>` in the source repository before deletion.
   Delete without a confirmation after the rescue succeeds.
7. **D7 — Source enforcement.** A source test rejects `mkdtemp`, `git clone`, and `git worktree add` outside the maker function.
8. **D8 — Disk alarm.** The disk-floor temporary sweep becomes an alarm without deletion rules.
   The stall watch stops hung owners.
9. **D9 — Review folders.** Remove `~/rust/repro` row folders after the row verdict and the rescue ref.

## Implementation contract

1. Extend `scripts/lib/build_folders.py`; do not add another maker.
   Keep `create_build_folder()` as the sole physical allocation function.
   Put repository initialization, clone, and worktree creation inside that function.
   Use its existing Python API or CLI for local, shell, JavaScript, and remote callers.
2. Build the human name from explicit showrunner, unit, work, and purpose arguments.
   Keep stable physical slot paths and stable Cargo target paths.
   Keep the human name in the lease and caller-visible output without requiring another link tree.
3. Use Python `fcntl.flock` on a lock file beside the folder or slot.
   Keep the lock inode outside every reset or deletion boundary.
   Acquire the lock before exposing the folder.
   Pass the descriptor to the working process and its children.
   A held lock proves ownership; metadata only supplies attribution.
4. Serialize acquisition, rescue, source reset, target removal, and folder deletion under that lock.
   Python callers pass the held lease descriptor to build and test children.
   Slot recovery removes a stale index lock under the lease.
   Slot acquisition replaces target symlinks with directories inside the slot.
   Explicit Cargo output settings remain unchanged.
   Nested slots replace only defaults inherited from an enclosing slot.
   Preserve commits, refs, staged changes, unstaged changes, untracked source, ignored source, conflicts, and nested repository work.
   Publish the rescue objects under a unique `refs/rescue/<name>` in the source repository.
   Verify the rescue before deletion; retain the folder when rescue fails.
   Workspace removal exports clean local branches, tags, and every stash reflog entry before it deletes private Git metadata.
   When no local source remains, export the rescue into a sibling bare repository named `<workspace-root>-rescues.git`.
   Incomplete slot recovery retains repositories with a different object store.
   Generated Cargo target data does not need a source snapshot.
   An ignore rule alone does not identify generated data.
5. Use one pool for each machine and repository name (the user, 2026-11-15).
   The pool word is the repository name from the origin URL, with no exception table. A repository with no origin uses its main checkout folder name.
   Different worktrees of one source repository share that pool.
   A run takes the lowest free number. When all slots are held, the pool makes the next number, up to the pool's maximum.
   At the maximum, the run waits in the build queue.
6. Measure the complete slot size when taking the slot.
   Rescue before resetting source or removing an over-limit target.
   Remove the target once, then run one cold build.
   Report a source-only excess without a repeated deletion loop.
7. Use one-offs only for temporary files outside repositories.
   Keep fixture repositories in private slots, separate from file-only fixture roots.
   Rescue an unexpected repository before removing its folder.
8. Make legacy collectors respect held leases in Phase 2, before any migrated caller activates.
   Acquire the same lease lock before a collector judges or deletes a managed folder.
   Keep old guards for legacy folders until their callers drain.
   Convert the disk-floor sweep to alarms only in Phase 8.
9. Preserve admission, memory, account, cancellation, and result-delivery behavior during migration.
   Drain old users before adopting their cache paths.
   Keep the stall watch's stop decision separate from proof that a slot is free.

## Configuration and user decisions

The builds queue unit owns `config/build_folders.conf`.
The user approved the slot values on 2026-11-12 (relayed by builds). On 2026-11-14 the user deferred slot work until the build disk is in (#88). The disk is now in, and the Mac counts stand. Count x complete size limit:

| Machine | Repository | Slots | Size limit |
| --- | --- | ---: | ---: |
| natedev | hana | 4 | 40 GiB |
| natedev | hana merge gate | 1 | 120 GiB |
| natedev | cargo-liner | 6 | 15 GiB |
| natedev | claude | 16 | 2 GiB |
| natedev | tool-based-ui | 2 | 25 GiB |
| natedev | every other repository | 1 | 20 GiB |
| Mac | hana | 2 | 40 GiB |
| Mac | cargo-liner | 2 | 15 GiB |
| AWS | none (per-run boxes keep no cache) | 0 | - |

The values live in `config/build_folders.conf` as `slots.<machine>.<repository>.count` and `slots.<machine>.<repository>.max_bytes`.
They change only through /tune and apply at the next slot take, with no deploy.
`roots.slots.<machine>` keeps a suitable existing root. Existing `names.*` and `patterns.*` settings stay.
The cutover on Thu 11-19 deletes `copies.root`, `roots.scratch*`, `roots.copies*` and the `prefixes.*` keys. The delete of `copies.root` needs a separate go from the user.
Missing required capacity values stop admission with a named config error.
Tests use explicit private values under the same schema; they do not set production limits.
The private-config helper supplies test roots and values, not another allocation API or fixture registry.
Do not add per-module production fixture keys or a parent lease that blocks its child fixture slots.
Mac hana's approved count remains two; its size limit still needs user go.

### Slot values after the build disk (2026-11-14)

#88 is closed with no limits set. The user, 2026-11-14 12:15: "We should only implement this after we install the disk." The build disk is now in. The values are measured again on `/build` and go to the user for approval.

The user questioned the first #88 values: "Why are we allocating more than we have? And why have 16 Claude folders?" After Phase 8, nothing deletes at the floor, so the sum of count x size limit on each machine must fit its budget. builds set these rules on 2026-11-14 for the next measure:

1. A machine's budget is the space that build folders use at the measure, plus the free space above the floor.
2. A count is the measured 7-day peak of tasks of that repository that built at the same time on that machine (build log). A count the user approved stays: Mac hana and Mac cargo-liner stay at 2.
3. A size limit is the largest measured folder plus a small margin. When the budget does not hold that, the proposal names the repository that gets less and the cost.
4. Pools are keyed by the origin URL as owner/name. Every worktree of one origin is in one pool. A repository with no origin uses its shared git folder.
5. Each repository is named. No line covers "every other repository".
6. A repository with count 0 waits for a slot and prints one line that names the repository and the /tune key to set. A missing key still stops admission with a named config error.
7. The claude folders are on tmpfs. They count against memory, not the disk sum.
8. When a slot take deletes a target above its size limit, verify.sh prints one line that names the slot and its measured size. The target is deleted once and the next build starts from nothing; it is not made smaller.

### Slot proposal on /build (2026-11-15, waits for the user)

Measured on natedev 2026-11-15 00:59 to 01:04 EST. The Mac values of 2026-11-14 stand.
A slot is one fixed workspace path. Its build-dir follows from its folder name, `/build/cargo/<name>`, so a slot folder name is unique across `~/rust` and `~/worktrees`.
A size limit covers the build-dir and the workspace `target/` together. The budget check is per disk.

| Machine | Repository | Peak | Count | Largest folder | Size limit | Sum |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| natedev | hana (all natepiano/hana worktrees and gate caches) | 7 | 7 | 74.1 GiB | 78 GiB | 546 GiB |
| natedev | cargo-liner | 5 | 5 | 30.2 GiB | 32 GiB | 160 GiB |
| natedev | bevy_brp | 0 | 1 | 18.4 GiB | 20 GiB | 20 GiB |
| natedev | nateroids | 0 | 1 | 16.7 GiB | 18 GiB | 18 GiB |
| natedev | obsidian_knife | 0 | 1 | not measured | 3 GiB (estimate) | 3 GiB |
| natedev | gltf-ibl-sampler-egui | 0 | 1 | not measured | 5 GiB (estimate) | 5 GiB |
| natedev | claude (on /build since 2026-11-14) | 11 | 11 | 0.05 GiB | 0.25 GiB | 2.75 GiB |
| Mac | hana | 1 | 2 | 63.8 GiB | 66 GiB | 132 GiB |
| Mac | cargo-liner | 1 | 2 | 24.7 GiB | 26 GiB | 52 GiB |

Sums:

1. natedev `/build`: pools 755 GiB plus sccache 26 GiB is 781 GiB. The budget is 817 GiB used plus 2117 GiB free above the 733 GiB floor (20 percent), 2934 GiB.
2. natedev `/`: final outputs in `target/` are at most 2.6 GiB for each slot, about 25 GiB, against 283 GiB free above the 300 GiB floor.
3. Mac: 184 of 188.4 GiB, as on 2026-11-14.

## Rollout and estimates

Hours include implementation, focused tests, review fixes, and owner coordination.
Hours exclude user decision time, machine admission waits, and cloud runtime charges.
**B** marks a builds-owned file or file group throughout this plan.
The named builds unit reviews changes to each B file before landing.
Devtools owns all other files.
Coordinate shared files through sdlc; phase ownership does not transfer file ownership.

| Phase | Deliverable | Owner unit | Hours | Depends on |
| --- | --- | --- | ---: | --- |
| bf1 | Maker and process lock; landed | devtools; queue reviews B files | 6 | None |
| 2 | Rescue and legacy collector protection | queue; resources and devtools own collector edits | 8 | bf1 |
| 3 | Slots, limits, and queue fields | queue; resources wires capacity | 10 | 2; values measured again on `/build` and approved |
| 4 | Gate caches become slots | queue and aws | 8 | 3 |
| 5 | Other repository callers | verify, style, and devtools | 8 | 4 |
| 6 | One-offs and review folders | devtools; builds owners edit B files | 6 | 5 |
| 7 | Remaining fixtures and source enforcement | devtools; builds owners edit their fixtures | 8 | 6 |
| 8 | Stall watch and alarm-only sweep | resources and devtools | 6 | 7 |
| 9 | Test run folders; runs beside Phase 2 | devtools | 4 | bf1 |
| **Total** | | | **64** | |

### Schedule to the cutover (queue, 2026-11-15 22:33 EST)

The dates are the earliest when each phase starts the hour the phase before it merges. A phase with no owner run has no real start yet.

| Step | Owner unit | Kind | Start | ETA | Assigned |
| --- | --- | --- | --- | --- | --- |
| Phase 4 with slot limits (batch656, `e75b8ec06`) | queue | Code that merges | done 11-15 | merges Mon 11-16, after batch648 | yes |
| Phase 5 — other repository callers | verify (`verify.sh`, mirrors), style (style callers), devtools (unit and recovery worktrees) | Code that merges | Mon 11-16 | Tue 11-17 evening | **no** |
| Phase 6 — one-offs and review folders | devtools; queue, verify and report edit their B files | Code that merges | Tue 11-17 | Wed 11-18 midday | **no** |
| Phase 7 — remaining fixtures and source test | devtools; each builds unit migrates its own fixtures | Code that merges, one dispatch for each owner and folder | Wed 11-18 | Thu 11-19 morning | **no** |
| Phase 8 — stall watch and alarm-only sweep | resources (sweep and alarm), devtools (stall watch, folder audit) | Code that merges | Thu 11-19 | Thu 11-19 evening | **no** |
| Gate folders delete | resources | One-time run on natedev: deletes each `~/worktrees/gates` folder whose lock has no holder | Thu 11-19 | Thu 11-19 | yes; builds asked resources to add it to its plan |
| `copies.root` delete | queue (owns `config/copies.conf`) | Code that merges | after the user's go | Thu 11-19 at the earliest | yes; one user question for all deletes |
| Other config key delete | queue (owns `config/build_folders.conf`) | Code that merges: deletes `roots.scratch*`, `roots.copies*`, `prefixes.*` | after Phase 8 merges and the user's go | Fri 11-20 at the earliest | yes; same user question |

On `e75b8ec06`, code outside tests still reads each key the cutover deletes:

1. `roots.scratch*` and `roots.copies*`: `scratch.py`, `copy_record.py`, `tmp_sweep.py`, `pre-tool-use-scratch-guard.py`, `shot_report/transcripts.py`. Phases 6 to 8 move them.
2. `prefixes.*`: `build_folders.py`, `style/crate_run.py`, `style/style_footprint.py`, `step_estimator.py`, `aws_target/ledger.py`, `pre-tool-use-build-hold-guard.py`. Phases 5 and 7 move them.
3. `copies.root`: no reader. `copies.gates_root()` returns the slot root since Phase 4. Only the user's separate go holds this delete.

So the delete of `roots.scratch*`, `roots.copies*` and `prefixes.*` cannot run on Thu 11-19. The gate folders delete and the `copies.root` delete depend on Phase 4 alone. They stay on Thu 11-19.

Run the phases in this order.
Migrate adjacent fixtures with their callers during Phases 4–6.
Phase 7 completes the fixture audit and closes alternate allocation paths.
Phase 8 is last; enable the stall watch before removing disk-floor deletion rules.

## Maker inventory by phase

These entries name current makers and lifecycle consumers, not every textual match.
Line references describe the inspected tree; recheck them before implementation.
Each phase also owns the adjacent tests for its files.
All builds-owned adjacent tests carry the same B owner as their implementation.

| Phase | Makers or consumers | Owner |
| --- | --- | --- |
| bf1–3 | **B:** `scripts/lib/build_folders.py`, `scripts/lib/test_build_folders.py`, `config/build_folders.conf` | queue |
| 2, 8 | **B:** `scripts/lint/tmp_sweep.py`, `scripts/lint/test_tmp_sweep.py`, `scripts/lint/sweep.py`, `scripts/lint/test_sweep.py` | resources |
| 2, 8 | `scripts/lint/folder_audit.py`, `scripts/lint/test_folder_audit.py`; cleanup in `scripts/lib/scratch.py` | devtools |
| 3 | **B:** `scripts/build_queue/queue_view.py`, `scripts/build_queue/memory_feed.py`; `scripts/build_targets/build_targets.py` only for required capacity wiring | queue for queue files; resources for target files |
| 4 | **B:** `scripts/copies/copies.py`; gate pools and natedev caches | queue |
| 4–5 | **B:** `scripts/mac_test/offload.py`, `scripts/mac_test/linux_runner.py`, `scripts/mac_test/mirror_root.py` | verify; queue coordinates shared gate slots in Phase 4 |
| 4 | **B:** `scripts/aws_target/gate_box.py`, `cloud_gate.py`, `gate_trial.py`, `box.py` | aws |
| 4 | `scripts/production/merge_gate.py`, `ci_points.py`, `waiting.py`, `bump_gate.py`, `fast_track.py` | devtools; queue coordinates gate adapters |
| 5 | **B:** `scripts/direct/verify.sh`, `scripts/mac_test/copy_watch.py`, `scripts/mac_test/loopback_proof.py` | verify |
| 5 | **B:** `scripts/style/crate_run.py`, `bulk_run.py`, `style_plan.py`, `style_sync.py`, `add_style.py` | style |
| 5 | `scripts/worktree/make_worktree.sh`, `worktrees.py`, `commands/make_a_worktree.md`, `scripts/production/add_unit.py`, `scripts/agents/shutdown.py` | devtools |
| 5 | `scripts/bevy_migration_plan/bevy_migration_ensure_repo.sh`, `scripts/direct/simplify_daily.py`, `scripts/buildlog/rust_release.py` | devtools |
| 5–6 | `scripts/release/resolve_path_pins.sh`, `scripts/new_rust_project/rust_generate.sh`, `scripts/berth/install/install.sh`, `scripts/production/smoke_launch.sh` | devtools; use slots when content includes repositories |
| 6 | `scripts/lib/scratch.py`, `scripts/production/change_review.py`, `commands/change_review.md`; `~/rust/repro` row folders | devtools |
| 6 | **B:** file-only allocations in the Phase 4–5 files; `scripts/style/style_ledger.py`, `style_footprint.py`, `style_proposals.py` | queue, verify, aws, or style according to the file above |
| 6 | **B:** `scripts/build_queue/ci_cargo_front.py`, `memory_page_proof.mjs`, `queue_place.py` | queue |
| 6 | `scripts/review_links/change_page.py`, `branch_page.py`, `scripts/whoami/profile_notes_pass.py`, `scripts/lib/models_registry.py` | devtools |
| 6 | `scripts/production/merge_checkpoint.py`, `unit_config.py`, `scripts/usage/collect.py`, `scripts/hana_shot/hana_shot.py` | devtools |
| 6 | `scripts/test_times/coverage_overlap.py`, `module_cost.py`, `module_run.py`; use slots for repository content | devtools |
| 6 | `scripts/lint/style_review.py`, `scripts/hooks/checkpoint_gate.py`, `session-start-temp-root.py`, `scripts/direct/demo_refusals.sh`, `scripts/validate_and_push/ci_tick.sh` | devtools |
| 7 | `scripts/production/test_merge_checkpoint_validation.py:248`, `scripts/production/cli_fixture.py:146`, `scripts/lint/memory_test_support.py:95` | devtools |
| 7 | `scripts/production/staff_test_support.py`, `merge_gate_test_support.py`, `scripts/berth/tests/installed_front_end.py` | devtools |
| 7 | **B:** `scripts/direct/recorder_fixture.py`, `scripts/build_queue/queue_fixture.py`; all remaining builds fixtures | verify or queue; other builds fixtures retain their runtime owner |
| 7 | `scripts/direct/assign_launcher_test_support.py`, `scripts/berth/tests/check_installed_hooks.py`; **B:** `scripts/style/test_style_ledger_tickets.py` | devtools; style owns the B file |
| 7 | Remaining Python, shell, and JavaScript fixtures under `scripts`; maker instructions under `commands` | devtools; builds fixtures retain their runtime owner |
| 9 | `scripts/production/targeted_test.py`, the shared test bootstrap under `scripts/lib`, and their tests | devtools |
| 8 | `scripts/production/stall_watch.py`, `scripts/production/test_stall_watch.py`; **B:** `scripts/build_targets/working_set.py` as a process identity reference | devtools; resources owns the B reference |

## Phase bf1 — Maker and process lock

**Status:** landed on main.
**Goal:** Implement D1 and D5 without moving callers.
**Owner unit:** devtools implements the current dispatch; builds queue reviews and retains ownership of the B files.
**Estimate:** 6 worker hours.
**Files:** The bf1–3 inventory row; no caller migration files.
**Dispatch:** `/tmp/claude/unit/bd19678b-c7b1-4e24-be9e-133ecfe24c7a/phases/bf1/prompt.md` remains authoritative for this phase.

1. Extend the existing `create_build_folder()`, `temporary_build_folder()`, and `run_build()` implementation.
2. Build `<showrunner>-<unit>/<work>-<purpose>` from explicit arguments.
3. Acquire a Python `fcntl.flock` before exposing the folder.
4. Keep the lock beside the folder so deletion preserves its inode.
5. Pass the held descriptor through `run_build`, the CLI, and working descendants.
6. Record showrunner, unit, work, purpose, pid, and start time beside the lock.
7. Return held or free from a query function without checking session liveness.
8. Keep every current caller working without moving callers in this phase.

The owner of a folder owns its whole subtree.
Parent removal removes every nested folder and record, whether held or free.
A process must release a nested folder before its parent context ends.
Keeping a nested folder after the parent context ends is a caller error.
Each nested context removes its own folder, lease, and lock when it ends first.

An asynchronous exception, such as `KeyboardInterrupt`, between cleanup bytecodes can leave the lock held until the process exits.
The kernel then releases the lock.
The folder becomes free recorded residue for Phase 9 cleanup.
The maker adds no signal handler or signal mask to close these bytecode windows.

**Acceptance gate:** Add tests for mutual exclusion, child inheritance, release after process death, and explicit names.
Show that these tests fail before the change.
Require basedpyright to report zero errors and zero warnings on changed files.
Run `scripts.lib.test_build_folders` and every caller's test modules as one detached run.
Discover callers of all three existing functions before selecting those modules.
Do not add slots, rescue refs, or size limits during bf1.

## Phase 2 — Rescue and legacy collector protection

**Goal:** Preserve Git work and protect every held lease from old deletion rules.
**Owner units:** queue owns rescue; resources owns `tmp_sweep.py`; devtools owns `folder_audit.py` and scratch integration.
**Devtools part (bf2d):** `build_folders.collector_lease` holds a managed folder's lease lock without waiting.
`scratch.removal_lock` and `scratch.judgment_lock` take it first and hold it through judgment and deletion; a held lease is busy.
The folder audit, the tmp sweep, scratch end, the floor sweep and copy removal get the guard through these locks.
**Held child leases (bf2p):** `removal_lock` also holds every lease lock below the folder through deletion; a held one keeps the parent.
**Lease records (bf9r):** a lock file or lease record is judged by its folder's lease; a live owner's records are busy.
`tmp_sweep.py` still reports the old busy reason; resources can use `scratch.BUSY_REASON`.
**Estimate:** 8 worker hours.
**Depends on:** bf1.
**Files:** Maker files, `scripts/lib/scratch.py`, and the Phase 2 collector inventory rows with their tests.

1. Add rescue and deletion under the same held lease lock.
2. Preserve the Git work listed in the implementation contract under `refs/rescue/<name>` in the source repository.
3. Verify the rescue objects and ref before reset or deletion.
4. Retain the source when rescue fails; do not request another confirmation after a successful rescue.
5. Make `tmp_sweep.py` and `folder_audit.py` acquire the lease lock before judging or deleting a managed folder.
6. Treat a held lease as busy even when legacy session, cwd, and `.claude-busy` checks permit deletion.
7. Preserve existing guards for folders that do not yet use the maker.

**Acceptance gate:** Restore commits, index changes, working files, ignored source, conflicts, and nested repository work from rescue refs.
Inject a rescue failure and require the source to remain.
Run each legacy collector against a held lease with no legacy busy marker.
Require both collectors to retain that folder.
Check the lock across judgment and deletion to prevent a race with acquisition.
Run maker, scratch, temporary sweep, and folder audit tests before Phase 3 activates leases.

## Phase 3 — Slots, limits, and queue fields

**Goal:** Implement D2–D4 through the maker and existing queue page, with the user's slot rules of 2026-11-15.
**Owner units:** queue implements slots and queue fields; resources wires capacity.
**Estimate:** 10 worker hours.
**Depends on:** Phase 2 collector guards; the rescue function (built as bf3r, because Phase 2 left it unbuilt); user go before production activation.
**Files:** Maker/config files, `scripts/lib/git_rescue.py`, and the Phase 3 queue/target inventory row with adjacent tests.

The user's rules (2026-11-15, relayed by builds):

1. A slot is `/build/slot/<repo>-slot<N>` on natedev and `~/build/slot/<repo>-slot<N>` on the Mac. Its Cargo output is `/build/cargo/<repo>-slot<N>`.
2. The repo word is the repository name from the origin URL for every repository, with no exception table.
3. Every pool grows on demand. A run takes the lowest free number. When all slots are busy, the pool makes the next number, up to a maximum for each pool. At the maximum, the run waits in the build queue.
4. Once a night, a slot unused for 7 days is deleted with its Cargo output. The maximum for each pool and the 7 days are config values.
5. A slot reset uses git and rewrites only the files that changed. It never deletes and copies.
6. The holder and the purpose show on the queue page and in the lease, never in the path.

Steps:

1. bf3r: `git_rescue.rescue()` saves all Git work in a slot under `refs/rescue/<name>` in the source repository and verifies the ref.
2. A slot is a detached `git worktree` of the repository's main checkout, so a commit needs no fetch and the source keeps its objects.
3. Take: lock the lowest free number; make the next number up to `slots.<machine>.<repo>.max`; a repository with no key uses `slots.<machine>.default_max` (1) and prints one line with the /tune key. No pool waits forever.
4. Reset: rescue, then `git checkout --force --detach <commit>` and a git clean of untracked files. The Cargo output stays.
5. Size: above `slots.<machine>.<repo>.max_bytes`, delete the Cargo output once and print one line with the slot and its size.
6. Lease: showrunner, unit, purpose, machine, commit, pid, start time and last use.
7. Nightly: rescue, then delete each slot unused for `slots.unused_days` days, with its Cargo output.
8. The Builds page Running table shows the slot number in a Slot column after vCPU (the user, 2026-11-15). The unit column already names the holder. The lock, not the lease, proves a holder.
9. One small private-config helper for tests that call the maker directly.

`/build/slot` needs a nixos tmpfiles line and a user rebuild; that line is part of the migration item. Until then, tests and proof point `roots.slots.<machine>` at a test root. Merge gates, targeted tests and copies.py clones move to slots in Phase 4.

**Acceptance gate:** Test the lowest free number, growth to the maximum, the wait at the maximum, the default maximum, source reset with only changed files rewritten, Cargo output retention, one delete above the size limit, the 7-day delete with rescue, and a failed rescue that keeps the slot.
Verify queue attribution for every held slot without using metadata as proof of life.
Use isolated config values for tests; require user go before any production pool activates.

## Phase 4 — Gate caches become slots

**Goal:** Replace copies gate pools and natedev, Mac, and AWS gate caches with the fixed slots.
**Owner units:** queue owns copies and gate pools; aws owns AWS files; verify owns Mac execution files.
Devtools owns production gate adapters.
**Estimate:** 8 worker hours.
**Depends on:** Phase 3.
**Files:** All Phase 4 inventory rows and their adjacent tests.

1. Drain legacy cache users before adopting their paths.
2. Replace gate allocation and release with maker acquisition and lease release.
3. Retain stable Cargo targets while resetting source for the requested revision.
4. Pass the lease descriptor into local and remote working processes.
5. Route destructive cache operations through rescue under the lease lock.
6. Migrate adjacent gate fixtures through the maker and private config.
7. Make `copies.standing()` and the gate pool take slots inside `copies.py`. Its callers, including style sync and the toolchain trial, stop writing under `~/worktrees/gates` with no caller change.
   `copies.workspace()` does not take a slot: it is a named, editable checkout on a branch, and a slot reset would destroy its edits. It becomes an editable maker folder under `roots.workspace`.
   `copies.clone()` keeps its return-a-path API, so it cannot hold a slot lease. It becomes a maker folder outside `~/worktrees/gates`; its callers move to slots in Phase 5 (builds agreed 2026-11-15).
   A full pool waits in the build queue for at most `slots.wait_seconds`, and the run shows in the Queued table.
8. Point the read-only `copies.gates_root()` users (`rust_release.py` disk check, `lint/sweep.py` floor roots) at the slot root. Delete the `copies.root` key only after the user's separate go.
9. Add a test that no test writes under the live slot or gate roots.

Resources deletes `~/worktrees/gates` on cutover day, after Phase 4 is on main. It deletes each gate folder only when its lock has no holder.

**Acceptance gate:** Run copies, production gate, and affected Mac/AWS tests using isolated transports.
Verify cache reuse, revision reset, full-pool admission, cancellation, and lock release after failure.
Require the same lease fields on queue rows across all three machines.
Do not provision real machines or reset live caches during tests.

## Phase 5 — Other repository callers

**Goal:** Move remaining repository work into slots without changing task behavior.
**Owner units:** verify owns `verify.sh` and mirrors; style owns style callers; devtools owns unit and recovery worktrees.
**Estimate:** 8 worker hours.
**Depends on:** Phase 4.
**Files:** All Phase 5 inventory rows and their adjacent tests.

1. Route verify and mirror creation through the maker on each execution machine.
2. Route style base, worker, validation, and standing sources through slots.
3. Route explicit worktrees, unit worktrees, recovery worktrees, and remaining repository callers through slots.
4. Classify staging callers by content; repository content requires a slot.
5. Migrate adjacent fixtures with their runtime owners.
6. Preserve revision selection, cancellation, result delivery, and recovery behavior.

**Acceptance gate:** Run affected verify, mirror, style, worktree, unit, and recovery tests.
Prove that reused targets survive source reset.
Prove that cancellation releases the working process lock.
Require rescue before each replaced reset or repository deletion path.
Verify owns mirror execution; resources does not own this migration.

## Phase 6 — One-offs and review folders

**Goal:** Centralize file-only temporary folders and implement D9 at the row verdict.
**Owner unit:** devtools coordinates; builds owners edit their B files.
**Estimate:** 6 worker hours.
**Depends on:** Phase 5.
**Files:** All Phase 6 inventory rows, their adjacent tests, and `commands/change_review.md`.

1. Route file-only scratch, browser profiles, command output, and temporary-file parents through maker one-offs.
2. Keep ordinary child directories inside their existing allocation.
3. Route repository content discovered during this migration through slots.
4. Remove `~/rust/repro` row folders after the row verdict and a verified rescue ref.
5. Retain a row folder when rescue fails.
6. Migrate adjacent fixtures through the maker and private config.
7. Give each one-off Cargo build-dir a lease with its workspace. On natedev, Cargo puts the build-dir of a workspace at `/build/paths<workspace path>`, outside the workspace. The release that removes the workspace also removes that build-dir. On 2026-11-15, 22 gate copies left 283 GiB of build-dirs under `/build/paths/tmp` with no workspace.

**Acceptance gate:** Run affected scratch, review, and temporary-file caller tests.
Verify that the removal of a one-off workspace removes its build-dir outside the workspace.
Verify lease inheritance for shell and browser workers.
Verify row-verdict cleanup after rescue and folder retention after rescue failure.
Verify that no one-off request silently contains repository work.

## Phase 7 — Remaining fixtures and source enforcement

**Goal:** Complete test migration and enforce D7 without another fixture allocation layer.
**Owner unit:** devtools owns the audit and enforcement; builds owners migrate their remaining fixtures.
**Estimate:** 8 worker hours.
**Depends on:** Phase 6.
**Files:** Phase 7 inventory rows and remaining executable makers found by the audit.
Add `scripts/lib/test_build_folders_sources.py`.
Update **B:** `scripts/copies/copy_guard.py` and `test_copy_guard_sources.py` under queue ownership.
Update `scripts/hooks/pre-tool-use-scratch-guard.py`, `test_scratch_guard.py`, `commands/unit/direct.md`, and `commands/showrunner/produce.md` as needed.

1. Audit `scripts` and `commands` for allocation operations, including Git argument vectors and generated remote commands.
2. Migrate `test_merge_checkpoint_validation.py:248`, `cli_fixture.py:146`, and `memory_test_support.py:95` explicitly.
3. Audit Git initialization and repository-copy helpers as well as clone and worktree commands.
4. Migrate remaining Python, shell, and JavaScript fixtures directly through the maker.
5. Use private slot config for repository fixtures and one-offs for file-only fixtures.
6. Reject executable `mkdtemp`, `TemporaryDirectory`, directory `mktemp`, clone, and worktree-add operations outside `create_build_folder()`.
7. Detect Git argument vectors, aliases, shell text, and generated remote command text.
8. Keep comments, imports, annotations, and inert examples separate from executable makers.
9. Remove allocation adapters that bypass the maker; permit no per-file allocation allowlist.

**Scope (builds, 2026-11-12):** The source test fails on bare `mkdtemp`, `git clone` and `git worktree add` outside the maker.
`TemporaryDirectory` as a context manager and pytest tmp fixtures stay, because they delete themselves.
**Dispatches:** Split the migration into one dispatch for each owner unit and each scripts folder.
Each dispatch names its files and runs its own modules as its gate.
Run the source test last, after every dispatch lands.

**Acceptance gate:** Run the source test, existing command guard tests, and affected fixture modules.
Insert forbidden operations into temporary source inputs and require the source test to reject each operation.
Include vector-form Git clone, init, and worktree-add cases in the audit checks.
Confirm that shared fixture factories cannot create repositories outside maker slots.
Use this literal search as a starting point, not as the complete inventory:

```sh
rg -n 'mkdtemp|TemporaryDirectory|git clone|worktree add|make_worktree' scripts commands
```

## Phase 8 — Stall watch and alarm-only sweep

**Goal:** Implement D8 after all callers and tests use the maker.
**Owner units:** devtools owns stall watch and folder audit; resources owns the disk-floor sweep and alarm.
**Estimate:** 6 worker hours.
**Depends on:** Phase 7.
**Files:** Phase 8 inventory rows, `scripts/lib/process_group.py` as a reference, and adjacent tests.
Update **B:** `docs/as-built/copies.md` under queue ownership and `docs/as-built/accounts-queue-panes.md` under queue/resources ownership.
Amend existing devtools owner documents when needed.

1. Apply the existing configured stall policy to held lease owners.
2. Verify process identity before stopping a hung owner and its working descendants.
3. Observe lock release before treating the slot as free.
4. Preserve held status when remote evidence is unavailable.
5. Enable this behavior before removing the disk-floor deletion rules.
6. Convert `tmp_sweep.py` and its floor callers to measurement and alarms without deletion.
7. Delegate eligible one-off cleanup to the maker's lock and rescue path.
8. Retire competing managed-folder deletion paths after legacy users drain.

**Acceptance gate:** Test productive work, hung owners, PID reuse, remote loss, and remaining descendants.
Require unrelated processes to receive no stop signal.
Require low-disk checks to retain every fixture folder.
Verify that explicit maker cleanup can still rescue and remove an unlocked one-off.
Run stall watch, process group, sweep, folder audit, and source enforcement tests.
Run the existing complete gate after the final migration.

## Phase 9 — Test run folders

**Status:** built in bf9 (609b743ff); bf9r moved the start to interpreter startup only.
**Implementation:** Direct unittest and timed runs allocate through the shared Python bootstrap.
The startup hook and the shim bootstrap start the folder; `package_root.py` selects the checkout only.
`targeted_test.py` uses the same maker context.
Each run holds its lock and sets TMPDIR for itself and its children.
Normal exit removes the complete allocation.
The next run sweeps free residue in the same test-runs parent before allocation.
The sweep retains held allocations and removes unfinished temporary lease files under the allocation lock.
`run_build` keeps its command in the caller's process group and installs no signal handler.
Stopped commands leave recorded residue for the sweep after their locks become free.

**Goal:** Give each test run outside the merge gate its own TMPDIR, so a killed run leaves no folder behind.
**Owner unit:** devtools.
**Estimate:** 4 worker hours.
**Depends on:** bf1 only; this phase runs beside Phase 2.
**Files:** The Phase 9 inventory row and adjacent tests.

Killed test runs leave `TemporaryDirectory` folders, for example `sender-guard-*` (`scripts/message/session_send_test_support.py:39`), `review-link-test-*` (`scripts/review_links/test_review_link.py:116`) and `tmp*` (`scripts/lint/test_tmp_sweep.py`).
The tests are correct: Python runs no cleanup on SIGTERM.
The merge gate already gives each run its own TMPDIR (`scripts/production/merge_gate.py:1204`).

1. Make one per-run folder through the maker in `targeted_test.py` and in direct unittest runs through the shared bootstrap.
2. Set TMPDIR to that folder for the run and its children. The run holds the folder's lease lock.
3. Remove the folder when the run exits.
4. Remove a killed run's folder when its lease lock is free. Add no signal handler (user rule).
5. Do not change the tests that make temporary folders.

**Acceptance gate:** Kill a run with SIGTERM and with SIGKILL; require its folder to be removed once its lock is free.
Require a live run's folder to stay.
Run the targeted test and bootstrap tests.

## Shared completion gate

1. Run focused tests for changed behavior and every affected caller.
2. Run basedpyright on changed Python files with zero errors and zero warnings.
3. Run `bash -n` on changed shell files.
4. Run `git diff --check`.
5. Run long validation through `scripts/agents/detached_runs.py start` and record the completed result before phase acceptance.
6. Obtain the named builds owner's review for each B file before landing.

Do not weaken existing assertions or test behavior to obtain a pass.
This planning task changes only this document and creates no commit.

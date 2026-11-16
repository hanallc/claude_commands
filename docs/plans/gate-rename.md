# Gate rename: merge_gate.py becomes gate.py, and targeted_test.py becomes a gate.py mode

> **Status: APPROVED 2026-11-16 — sdlc agreed. The user (through morty and builds) gave the go on the plan, the conf rename and "the gate" (13:47), the trial branch prefix `aws-trial` (15:49), and the stored names `gate` and `test-affected`, the mode `gate.py --affected` and the box user `gate-box` (17:23). natest starts chunk 1 after its phase 315 work.** Seven chunks. Each chunk merges alone as a builds item and needs no quiet time.

> **Request:** #108 part B, the user via morty and builds, 2026-11-16. Plan written by the verify unit.

## Request

The user: rename `merge_gate.py` to `gate.py`. On the sweep: "it needs to be planned out in chunks maybe as there
IS no quiet time unless we request a quiescence."

Three corrections from the user (via morty and builds, 2026-11-16):

1. No `targeted_tests.py` file. `gate.py` takes in the targeted test entry point as a mode: one tool, six jobs.
   The separate file goes away at the end of the rename.
2. The prose "merge gate" is swept too: "it doesn't only merge - we'd have to sweep this name through documentation
   and skills". Old plan docs stay as history.
3. `scripts/aws_target/gate_trial.py` becomes `aws_trial.py`: "gate_trail should probably change to aws_trial if
   it's not a gate". It stays a separate tool that aws owns.

The user's question: should `gate_trial` and `gate_modules` merge into `gate.py` as subcommands?

## Measured uses (git grep in ~/.claude at main 0d8d885fc, 2026-11-16 13:40 EST)

Lines that hold each form, by area. Plan docs of finished work are history and stay unchanged; they are in the
`docs` row only for the count.

| Area | merge_gate / merge-gate (files, lines) | targeted_test (files, lines) | prose "merge gate" (files, lines) |
| --- | --- | --- | --- |
| `scripts/production` | 32, 706 | 5, 17 | 17, 26 |
| `docs` (21 as-built, 29 plans, 4 other) | 44, 384 | 19, 56 | 33, 191 |
| `scripts/aws_target` | 11, 149 | 7, 31 | 2, 6 |
| `scripts/build_queue` | 10, 58 | 6, 19 | 4, 12 |
| `scripts/hooks` | 4, 43 | 1, 1 | 1, 1 |
| `scripts/lint` | 3, 40 | 1, 1 | 2, 2 |
| `scripts/buildlog` | 2, 14 | 2, 14 | 2, 4 |
| `config` | 6, 16 | 3, 6 | 2, 3 |
| `scripts/lib` | 5, 17 | 2, 3 | 0, 0 |
| `scripts/build_hold` | 3, 15 | 1, 2 | 2, 2 |
| `scripts/test_times` | 6, 17 | 0, 0 | 3, 23 |
| `scripts/maintenance` | 2, 3 | 0, 0 | 0, 0 |
| `commands` | 2, 2 | 0, 0 | 2, 3 |
| `scripts/message` | 1, 1 | 0, 0 | 0, 0 |
| `scripts/direct` | 0, 0 | 0, 0 | 2, 3 |
| `scripts/style` | 0, 0 | 0, 0 | 1, 1 |
| **total** | **131, 1,465** | **47, 150** | **73, 277** |

Of the 277 prose lines, 145 lines in 20 files are old plan docs, which stay. The prose sweep (chunk 5) has 132 lines
in 53 files: as-built docs 44, `scripts/production` 26, `scripts/test_times` 23, `scripts/build_queue` 12,
`scripts/aws_target` 6 (the AWS page's "Merge gates" label in `run_cost.py`), and 21 lines in the other areas.

Files named for the gate (17):

- `scripts/production/merge_gate.py` (2,837 lines), `targeted_test.py` (26 lines).
- `scripts/production/merge_gate_test_support.py`, `merge_gate_suites_test_support.py`.
- 13 test modules: `test_merge_gate.py`, `test_merge_gate_{holds,ledger,promotion,slot_sweep,suites,test_times}.py`,
  `test_merge_gate_suites_{admission,reruns,resources,signals,tripwires,types}.py`, plus `test_targeted_test.py`.
- `config/merge_gate.conf`.

Other names that carry the old name:

- 16 environment variables `MERGE_GATE_*` (about 80 uses in 25 files). `MERGE_GATE_PARENT_PID` crosses
  processes: the gate sets it, and `scripts/lib/python_startup/sitecustomize.py` reads it.
- The state folder `~/.local/state/merge-gate` (42 MB `affected-module-facts.json`, land queue and results,
  module history, nightly red records, `active-runs/`). `land_runs.py` and `merge_gate.py` read
  `MERGE_GATE_STATE_DIR` and `MERGE_GATE_PASSED_DIR`.
- Stored step names: `SuiteMode` writes `merge-gate` and `targeted-test` into buildlog call records and admission
  records. `admission_service.py`, `admission_records.py`, `lint/memory_admit.py`, `aws_target/run_cost.py`,
  `cloud_gate.py` and `gate_box.py` read them.
- Process-name matchers that must know both names during the change:
  `build_hold/build_hold.py` (`"merge_gate.py"`), `build_queue/admission_service.py` (`ADMISSION_CALLERS`),
  `build_queue/admission_code.py` (`ADMISSION_CODE_PATHS`, part of the admission code version),
  `hooks/pre-tool-use-build-hold-guard.sh` (`*merge_gate*|*targeted_test*`), `lib/suite_runners.py` (`SUITE_RUNNERS`).
- Outside the repository: `/etc/nixos/modules/linux/claude-nightly.nix` runs `merge_gate.py --nightly`.
- The AWS gate box imports `scripts.production.merge_gate` from the commit under test (`gate_box.py`, `cloud_gate.py`).

## Decisions

1. **The replacement term for the prose "merge gate" is "the gate"** (plural "gates"; the AWS page label
   "Merge gates" becomes "Gates"). The user confirms this term with the plan.
2. **The six jobs of `gate.py`:** the checkpoint merge (default), `--tests-only`, `--affected` (was
   `targeted_test.py`; the user named it 17:23), `--nightly`, `--promote-only`, and `nightly-assign` / `nightly-assigned`. `--affected`
   follows the existing flag form of `--tests-only` and `--nightly`.
3. **Stored step names (the user, 17:23, option D):** `merge-gate` becomes `gate`, and `targeted-test` becomes
   `test-affected`, the user's job name "test affected" (#109). Old records get a migration once at deploy
   (chunk 4); no reader keeps the old names. The config keys that hold the step names follow (13 keys on main
   8416aab51): `program_first_peak_gib.*` and `peak_hold_seconds.*` in `build_admission.conf`, `prefixes.*` in
   `build_folders.conf`, and `names.*`, `prose.*` and `owned.*` in `build_steps.conf`. Their values do not change.
   A change of a value (for example the folder prefix `merge-gate-`) needs its own go.
4. **`gate_modules` and `gate_trial` do not merge into `gate.py`.** See the answer below. `gate_trial.py`
   becomes `aws_trial.py` (chunk 7).
5. **Matchers use the exact base name.** `gate.py` is short, and a substring match also hits `cloud_gate.py` and
   `gate_box.py`. The hook guard's `*merge_gate*` pattern becomes an exact base-name test for `gate.py`.
6. **Environment variables become `GATE_*`.** No exact name collides today, but the AWS box already uses
   `GATE_TEST_{BIN,BLOCK,HOST,ROOT}` and the gate has `MERGE_GATE_TEST_{ISOLATION,PYTHONPATH,REPO,STATE,WAIT}`.
   Chunk 3 checks the full list again before it renames.
7. **The state folder moves to `~/.local/state/gate` once, at deploy** (user rule: migrate once at deploy). No
   symlink and no dual read stay after the move.

## Answer to the user's question: gate_trial and gate_modules

Recommendation: keep both as separate files.

- **`gate_modules.py`** (41 lines) exists so that light callers do not load the 2,837-line gate. 17 files import it:
  hooks, `buildlog/index.py`, `test_times/*`, `maintenance/herdr_upkeep.py`, `aws_target/cloud_gate.py` and others.
  As a part of `gate.py`, every one of those calls loads the full gate. That adds start time on every call (the Fast
  god) and buys nothing. It already carries the `gate_` prefix, so the names stay one family.
- **`gate_trial.py`** (497 lines) is the aws unit's tool. It compares local revisions on disposable AWS gate
  instances. It is a measurement tool, not the gate. As a subcommand it puts AWS trial code and its owner into
  `gate.py`'s surface (the Simple god: one tool, one job). It stays in `scripts/aws_target` with aws. The user
  agreed, and because it is not a gate, it becomes `aws_trial.py` (chunk 7).

## Chunks

Each chunk is one commit that merges alone through the gate, as a builds item. After chunk 1, the old and new names
both work until chunk 6, so no chunk needs quiet time. A gate that runs during a merge keeps the code it loaded.

The thin entry points are a transition only. Chunk 6 starts as soon as its three checks pass, on the same day that
the last chunk 2 commit merges. No old name stays after that.

Writers: natest writes chunks 1, 3, 4 and 6. devtools writes sdlc's chunk 2 commit. Each other owner writes its own
chunk 2 commit and its own part of chunk 5. aws writes chunk 7.

### Chunk 1 — gate.py with the targeted mode, old names as thin entry points · owner: sdlc (scripts/production) · writer: natest

1. `git mv` `merge_gate.py` to `gate.py`.
2. Add the `--affected` mode to `gate.py`. It does what `targeted_test.py` does: `run_tests_only` with the
   targeted suite mode, in a test run folder. `SuiteMode.MERGE_GATE` and `SuiteMode.TARGETED_TEST` become
   `SuiteMode.GATE` and `SuiteMode.TEST_AFFECTED`; their stored values change in chunk 4.
3. `git mv` the two test-support files to `gate_test_support.py` and `gate_suites_test_support.py`, and the 13
   test modules to `test_gate*.py`. `test_targeted_test.py` becomes `test_gate_affected.py` and calls
   `gate.py --affected`.
4. Make `merge_gate.py` a thin entry point: run as a script, it calls `gate.main()`; imported, it is the `gate`
   module (`sys.modules` alias). Make `targeted_test.py` a thin entry point that calls `gate.main()` with
   `--affected`.
5. Switch every caller inside `scripts/production` (`merge_checkpoint.py`, `land_runs.py`, `fast_track.py`,
   `affected_modules.py` and the tests) to the new names.
6. Add `gate.py` next to the old names in the five process-name matchers (decision 5). This touches `build_queue`
   and `build_hold` files; queue and resources agree the change. The admission code version changes, so queue
   confirms the service takes a version change at deploy.
7. Tests: every module that imports the gate (47 files) and the matcher tests.

### Chunk 2 — callers by owner · one commit per owner, any order

| Owner | Files |
| --- | --- |
| aws | `aws_target/cloud_gate.py`, `gate_box.py`, the trial tool (`aws_trial.py` after chunk 7), `deploy.py` and their tests; the box user and folder (see chunk 7) |
| queue | `build_queue/admission_service.py`, `admission_code.py`, their tests |
| natest | `test_times/*` (6 files) |
| report | `buildlog/*` (2 files) |
| resources | `build_hold/*`, `lint/*` (`memory_admit.py`) |
| sdlc (writer: devtools) | `hooks/*`, `lib/*` (`suite_runners.py`, `sitecustomize.py`), `maintenance/*`, `message/*`, `commands/*` |
| natedev (nixos owner, through its showrunner) | `claude-nightly.nix`: `merge_gate.py --nightly` becomes `gate.py --nightly` |

A caller of `targeted_test.py` calls `gate.py --affected`. Each owner also corrects its own as-built docs that name
the files (21 as-built files). Old plan docs stay.

### Chunk 3 — names inside the gate · owner: sdlc · writer: natest

1. `MERGE_GATE_*` environment variables become `GATE_*` in one commit. `sitecustomize.py` reads both
   `GATE_PARENT_PID` and `MERGE_GATE_PARENT_PID` until chunk 6, because a gate started before the deploy still
   sets the old name for its children.
2. `config/merge_gate.conf` becomes `config/gate.conf`. The keys do not change. This is a config change; see
   "The user's go".

### Chunk 4 — stored names, migration once at deploy · owner: sdlc · writer: natest

1. A deploy step takes the land queue lock and the gate lock, then renames `~/.local/state/merge-gate` to
   `~/.local/state/gate` in one `rename(2)` (same file system, atomic).
2. A gate that runs at deploy holds the gate lock, so the step waits for that one gate. That is not a quiet time:
   the land queue holds new gates for that wait only.
3. The same step rewrites the stored step names (decision 3) in the buildlog call records and the admission records.
   report (buildlog) and queue (admission) agree the migration of their records.
4. All readers use the new path and the new step names in the same commit: `gate.py`, `land_runs.py`, the readers
   that `git grep merge-gate` finds in `scripts/` (24 lines outside tests today), and the step-name readers listed
   above.
5. The step runs once. It does nothing when the old folder and old names are absent, and it refuses when both
   folders exist.

### Chunk 5 — the prose "merge gate" becomes "the gate" · one commit per owner, any order

1. Scope: live docs (as-built, README, `docs/production_format.md`, `docs/direct`), commands and skills, page text
   and messages. 132 lines in 53 files today; the count is taken again at the start.
2. Old plan docs of finished work stay as history.
3. Each owner sweeps its own files, in the same split as chunk 2. The AWS page label (aws) and the queue page text
   (queue) change with their owners' commits.
4. A page text change gets a visible Change-Review line with its screenshot.

### Chunk 6 — remove the old names · owner: sdlc · writer: natest

Start as soon as all three checks pass, on the same day that the last chunk 2 commit merges:

1. `git grep -E 'merge_gate|targeted_test|MERGE_GATE_'` in `scripts/`, `config/`, `commands/` finds only the
   two thin files and the matcher entries.
2. No live process runs `merge_gate.py` or `targeted_test.py` on natedev or AWS.
3. The natedev nixos switch with `gate.py --nightly` is live.

Then delete the thin `merge_gate.py` and `targeted_test.py`, the old matcher names and the `MERGE_GATE_PARENT_PID`
read in `sitecustomize.py`. After this chunk, `gate.py` is the only gate entry point.

### Chunk 7 — gate_trial.py becomes aws_trial.py · owner: aws · writer: aws · one commit, any time

The trial is a tool that aws runs by hand, so no thin entry point stays. On main b8f510804, `gate_trial` is in 90
lines of 9 files outside old plan docs.

1. `git mv` `scripts/aws_target/gate_trial.py` to `aws_trial.py`, and `test_gate_trial.py` to `test_aws_trial.py`.
2. Switch the callers: `deploy.py` (its `gate-trial` subcommand becomes `aws-trial`), `test_cloud_gate.py`,
   `test_gate_box.py`, `test_ledger.py`, `test_deploy.py` and `config/README.md`.
3. The trial branch prefix `trial_branch_prefix=gate-trial` in `config/cloud_gate.conf` becomes `aws-trial`. This
   is a changed config value; see "The user's go".
4. The prose "gate trial" becomes "AWS trial" in live docs (2 lines today). Old plan docs stay.
5. `config/README.md` lines 825–833 name six `gate_trial_*` keys that no conf file or script holds. aws removes
   those lines in the same commit.

The AWS box guest user `gate-trial` and its folder `/var/lib/gate-trial` are not part of chunk 7. Every
AWS gate run uses them, not only the trial. They become `gate-box` and `/var/lib/gate-box` (the user, 17:23) in aws's chunk 2 commit,
to match `gate_box.py` and `GATE_BOX_DIR`. The box makes the user at boot, so no image rebuild is needed. The risk
is one changed path in `config/test_times.conf` (`TOP_AWS_CHECKOUT`), which aws changes in the same commit.

## The user's go

1. The rename itself and the seven chunks of this plan: given 2026-11-16.
2. **The config file rename: `config/merge_gate.conf` becomes `config/gate.conf`** (chunk 3). The keys do not change. Given 2026-11-16.
3. **The replacement term "the gate"** for the prose "merge gate" (chunk 5): given 2026-11-16.
4. **The config value change: `trial_branch_prefix` becomes `aws-trial`** in `config/cloud_gate.conf` (chunk 7): given 2026-11-16 15:49 ("1 aws-trial yes").
5. **The stored step names** (decision 3): `gate` and `test-affected`, and the mode `gate.py --affected`: given 2026-11-16 17:23 ("Name 1 D").
6. **The AWS box user and folder name `gate-box`** (chunk 2, described under chunk 7): given 2026-11-16 17:23
   ("Name 2 gate-box").

The verify unit first wrote items 5 and 6 as approved when builds wrote "your calls stand". The user had not seen
them; morty then put them to the user. Decisions 5, 6 and 7 (exact matchers, `GATE_*` variables, the state
folder `~/.local/state/gate`) are unit calls; morty tells the user each one in one line.

## What sdlc agreed (2026-11-16, through builds)

1. The new names in `scripts/production`: `gate.py`, `gate_test_support.py`, `gate_suites_test_support.py`,
   `test_gate*.py`. The user's correction later replaced `targeted_tests.py` with the `--affected` mode.
2. The thin entry points are a transition only: the removal chunk (now chunk 6) starts as soon as its three checks
   pass, on the same day that the last chunk 2 commit merges.
3. Exact base-name matchers (decision 5). sdlc had also agreed that the prose stays; the user's correction replaced
   that with chunk 5.
4. The `GATE_*` environment names, and the config file rename (also the user's go).
5. The state folder move at deploy, under the land queue lock and the gate lock.
6. The edits in its other folders: `hooks`, `lib`, `maintenance`, `message`, `commands`.
7. natest writes chunks 1, 3, 4 and 6; devtools writes sdlc's chunk 2 commit. natest makes most gate changes
   today: of 118 `merge_gate.py` commits since 10-20, natest made 20, queue 18 and sdlc-devtools 16.

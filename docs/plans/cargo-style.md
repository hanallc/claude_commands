# cargo-style: the move and the direction for speed

Status: reviewed by dwight, 2026-11-16 (review 2 passed). Phases start in the order sdlc sets, after the main-red repair, which merged to main as 01a2815b0 on 2026-11-16.
Owner: style-unit (production sdlc). dwight guides and reviews. sdlc and cargo-liner keep each operations decision.

## Source

nate to dwight, 2026-11-16, 10:15 to 10:30 EST:

- "cargo-style - that's the family and it can then become a local cargo command: cargo style - with an api that we should optimize because i expect style management is something we need to get really good at having work very very fast - please guide style through getting this moved - help it create a plan and a direction for performance improvement".
- "i defintitely don't want it being built and tested as part of a claude_commands merge gate".
- "i dont' want to duplicate work that may already exist" (the Git reads of cargo-berth and the `syn` parsing of cargo-mend).

Inputs: dwight's brief `~/rust/hanadocs/dwight/cargo-style-brief-2026-11-16.md`, the facts file beside it, the cargo-liner showrunner's workspace needs, and the builds (aws-unit) answer on slots, install and leftovers.

Replaces: phases 6 to 12 of `docs/plans/style-refill-rust-store.md`. That plan's design sections (store choice, cache, selection contract, risks) stay the reference for the speed phases.

## Who does what

| Repo | Writer | Merger |
| --- | --- | --- |
| cargo-liner (`crates/cargo-style`) | style-unit, on its own branch in a `copies.py` clone of cargo-liner; paths claimed with cargo-berth | cargo-liner reviews and merges; its unit cargo-liner-style carries each checkpoint as a carried commit |
| `~/.claude` (Python client, Rust removal, docs) | style-unit | sdlc |

Settled by sdlc with cargo-liner, 2026-11-16 (sdlc message fb928cb8, which replaced cargo-liner's first split 0ac52304): style-unit writes `crates/cargo-style`; cargo-liner-style writes no code and carries each checkpoint; cargo-liner reviews and merges.

Where each Rust step of M1 and of every later cargo-liner phase runs (builds, aws-unit, 2026-11-16, message 915c25d9, read from the code):

| Step | Working folder | Build folder |
| --- | --- | --- |
| Edits | a `copies.py` clone of cargo-liner under `/build/claude/scratch/<task>`; no cargo and no verify.sh run in it directly | none |
| check, test, lint, release build | `copies.py run --repo <clone> --commit HEAD --apply-uncommitted <clone> -- ~/.claude/scripts/direct/verify.sh <step>` (drop `--apply-uncommitted` for committed work only; a release build adds `--publish <relative path>` to keep the binary) | a cargo-liner slot: source `/build/slot/cargo-liner-slot<N>`, output `/build/cargo/cargo-liner-slot<N>` (`copies.py:1027`, `build_folders.conf:39` 5 on natedev, `:42` 2 on the Mac). A full pool waits; it never falls back to scratch. |

The reader phases 1 to 5 ran verify.sh directly in scratch clones, so they built in `/build/cargo/scratch/<task>`, outside the slots. That is the "bypassing the slots" nate named. `copies.py run` ends it.

## Fixed rules

1. No Rust is built or tested in a `~/.claude` merge or gate, at any step.
2. Each main stays green at each step. The refill works during the whole move (nate, 2026-11-14).
3. Order: the crate merges and installs on natedev and the Mac; then the client switch in `~/.claude`; then the Rust removal from `~/.claude`.
4. Nothing goes from natedev to the Mac outside the build queue (nate, 2026-11-13).
5. No new public API in cargo-berth or cargo-mend in the move. Both are binary crates today. Reuse comes later, only with a measured need.
6. The `cargo style` API stays private: a binary, `publish = false`, no library target, until a real consumer shows what it needs.
7. A change to a test, a gate or a config value keeps its normal owner and approval. A changed or deleted config key needs nate's go.

## Decisions for nate

Working choices, taken by dwight on 2026-11-16: N1-A and N2-A. dwight reported both to nate, who can overrule them. The removal of the stand-in and of the parity tests in S8 needs nate's go, with the S8 switch.

### N1 — how the Python style tests get reader facts on the AWS gate machine

The AWS gate machine has no `~/.claude` home, no cargo-liner and no `cargo-style`. Today, after the main-red fix (01a2815b0), the env `STYLE_TEST_READER_SOURCE=python` makes each Python style test use the Python reader. A test that selects Rust gets the Python stand-in (`scripts/style/style_reader_oracle.py`) through the env `STYLE_READER_BINARY`. Nothing builds.

This is a temporary state. It ends at S8, when the Python selection goes. After S8 nothing uses the Python stand-in: the Python code that stays (the publication adapter and the ticket writers) gets selection results, not facts. Its tests then use a fake `cargo-style` on PATH that prints fixed replies, the same pattern as the fake `cargo-berth` in `test_refill_measure.py`. The stand-in, the Python fact code that only it uses, and the parity tests go with the Python selection in S8.

| Choice until S8 | What it does on the AWS gate and to the tests | Cost |
| --- | --- | --- |
| A. Keep the Python stand-in for tests only (recommended) | Tests pass on any machine with Python. The Python reader code stays, but the refill never runs it. | A second copy of the fact rules until S8. It can drift from `cargo-style`. The N2 rule catches a drift. |
| B. Install `cargo-style` on each gate machine | Tests read real Rust facts. | Each AWS gate box needs a Rust toolchain and a cargo-liner checkout, and a build or install time on each gate (about 10 min cold for this crate). That brings Rust back into the `~/.claude` gate, against fixed rule 1. |
| C. Recorded facts | Tests read fact files saved from a `cargo-style` run. | The records go stale when the fact rules change. A refresh step is needed, and a stale record hides a real change. |

Recommendation: A. It is the only choice with no Rust in the gate and no stale data. The drift risk is covered by the parity run in N2.

### N2 — the parity tests (`differential.rs`, `refill_parity.rs`)

They compare Rust facts with the Python reader. They need a `~/.claude` checkout. The cargo-liner gate must not reach outside its workspace.

| Choice | Effect |
| --- | --- |
| A. Move them until S8; each skips unless an explicit path variable names a `~/.claude` checkout. No gate runs these tests: the cargo-liner gate never sets the variable, and the `~/.claude` gate has no Rust. A phase rule runs them (below). They go with the Python selection in S8 (recommended). | Drift between the stand-in and `cargo-style` stays visible until S8. |
| B. Retire them now. | Less code. Until S8, nothing catches a drift between the stand-in and `cargo-style`. Removal of a test needs nate. |

Phase rule for A: a phase that changes fact code runs the parity tests with the path variable set, as a gate of that phase, before its checkpoint. Fact code is the Rust fact modules (`tokenizer`, `facts`, `declarations`, `module_scan`, `module_sites`, `name_sites`, `word_syntax`, `blob_reader`) and the Python files the stand-in reads facts from (`style_reader_oracle.py`, `declarations.py`, `style_footprint.py`, `approved_renames.py`). The move phases M1 to M3 change no fact code except the `sha2` upgrade in M1, which runs the rule once. S3 to S7 add selection, not facts; a phase that touches fact code runs the rule. A change to those Python files by another unit is not covered; style-unit reads the `~/.claude` main log for them at each of its phase starts and runs the rule on a hit.

Recommendation: A until S8. If nate picks N1-B or N1-C, B follows.

## Part 1: the move

### Phase M1 — `crates/cargo-style` in cargo-liner

**Repo:** cargo-liner. **Depends on:** the main-red fix in `~/.claude`.

1. Copy `rust/style_reader` to `crates/cargo-style`. Package and binary `cargo-style`; `publish = false`; `[lints] workspace = true`.
2. Use the workspace pins. Changes: `sha2` 0.10.9 to the workspace 0.11.0 (digest API change), `rayon` 1.11.0 to 1.12.0, `libc` to the workspace pin. `gix =0.89.0` is the same.
3. `cargo style` works: cargo passes `style` as the first argument; the binary accepts both forms.
4. `cargo-style --version` prints the crate version and the request protocol version (an integer). The Python client checks both (M2).
5. Parity tests: per N2. Git fixtures use `cargo-berth-test-support` where it fits.
6. An install script in cargo-liner, the same shape as cargo-berth's `install.sh`: staging target, `~/.cargo/bin`, rollback. cargo-liner's merge script runs `cargo install --path crates/cargo-style --locked` on natedev, under the memory admission of the build queue, only when the merge changed `crates/cargo-style` or `Cargo.lock`. Other merges pay nothing. The Mac installs only through `mac_queue_run.py --kind install`, on the same condition.
7. The crate joins the existing workspace CI job. No new job.

**Gates (every checkpoint, through verify.sh):** `cargo +nightly fmt`, `cargo mend` clean, clippy with the workspace lints, the style guide, nextest with each test under 1 s (natedev load under 32, Mac under 12). Change-Review line with an image on each checkpoint message.

**Build note (builds):** the first build of each new shape reserves 12 GiB once in the memory queue, then learns. The reader's peaks were 0.24 to 0.37 GiB. No new budget key.

**Milestone 3** when the crate is on cargo-liner main and installed on natedev and the Mac.

### Phase M2 — the Python client runs the installed `cargo-style`

**Repo:** `~/.claude`. **Depends on:** M1 installed on natedev and the Mac.

1. `scripts/style/style_reader.py` finds `cargo-style` on PATH. It runs `cargo-style --version` once per process. Two new keys in `config/style_reader.conf` (new keys need no go): `protocol` and `minimum_version`.
2. The check passes when the protocol is equal and the crate version is at least `minimum_version`. A newer install with the same protocol always works, so a cargo-liner merge never stops a refill.
3. A missing binary, an older version or a different protocol gives one clear error that names the install command. No build.
4. A protocol change runs in this order, so no refill stops:
   1. `cargo-style` learns the new protocol and keeps the old one; it answers the protocol the request names. cargo-liner merges and installs it on both hosts.
   2. `~/.claude` changes `protocol` and the client together. sdlc merges.
   3. A later `cargo-style` release drops the old protocol.
5. Remove the build on use: `VERIFY_SCRIPT` (`:31`), `_build` (`:425`), the build in `reader_binary` (`:451`), and the Git-tree stamp.
6. `STYLE_READER_BINARY` stays. Python tests use it per N1.
7. `reader=rust` stays. The refill now uses the installed binary. Rollback is `reader=python` (needs nate's go).

**Proof:** the refill runs on natedev and the Mac with the installed binary and the same result digest as before. The full style module set passes with tripwire cargo on PATH.

**Milestone 4** when this is on `~/.claude` main.

### Phase M3 — the Rust leaves `~/.claude`

**Repo:** `~/.claude`. **Depends on:** M2 on main, and one AWS gate with zero style failures.

1. Delete `Cargo.toml`, `Cargo.lock`, `rust/` and `taplo.toml` at the repo root. The generic gate code then finds no Rust: `hooks/checkpoint_gate.py:883`, `production/checkpoint_gate.py:83-109`, `merge_checkpoint.py:827-834`. No code change there.
2. Docs: `config/mac_test.conf:56-57` and `docs/direct/final_gate_commit.md:11-15` are true again with no edit. Mark the old reader plan as replaced by this plan.
3. Config, with nate's go: delete `install_folder` in `config/style_reader.conf`, then delete `~/.local/state/style/reader` (7.2 MiB).
4. Leftovers: delete the two scratch copies `reader-start-716d7d` and `reader-store-62d7e7` and their outputs under `/build/cargo/scratch` (1.3 GiB) through `scratch.py`. Clean the reader target in the Mac mirror of `.claude` through the Mac queue only.
5. Not in this phase: `config/cloud_gate.conf:111` keeps cargo and rustc on the AWS box. builds removes them only after a `gate_trial.py` pass without them, with nate's go.

**Proof:** one `~/.claude` merge and one checkpoint commit with no Rust step and no `verify.sh doc` pass; one AWS gate with zero style failures.

**Milestone 5** when this is on `~/.claude` main.

## Part 2: the direction for speed

### Phase S1 — measure today, set targets

**Repo:** `~/.claude` (the measure tool), then cargo-liner for `cargo-style` times. **Depends on:** the phase 6 checkpoint (the capture, replay and stage-measure tool, done and waiting).

Uses of a style production, each timed cold and warm with `refill_measure.py` on a frozen capture of cargo-liner-style:

| Use | Today | Target |
| --- | ---: | ---: |
| Cold refill (no saved results, new process) | 494 s (Rust facts, Python selection) | under 10 s |
| Warm refill, no change | 132 s | under 3 s (95th percentile) |
| Refill after one commit | not measured | under 3 s |
| Ticket assignment (`next-phase`) | not measured | under 3 s |
| Each other `cargo-style` call | not measured | measured in S1 |

The use that matters most: the refill after one commit. A style unit and its workers pay it at each ticket. S1 states where the time goes, by stage, with the phase 6 counters.

The targets in the table are the old plan's targets and are provisional. S1 replaces them:

1. For each use, measure the floor of each stage: process start, cache read, Git comparison, parse of the changed files, selection.
2. Set each target as a stated multiple of the sum of its floors.
3. For the refill after one commit, give the floor sum and say why the target is not under 1 s if it is not.

Open fact: the reader plan measured 37 s warm on 2026-11-14 with the Python reader; this table says 132 s. S1 states whether that is a different measure (the `refill_measure.py` warm run starts a new process on a cleared derived cache) or a slower warm path.

**Milestone 2** when the times and the targets are set.

### Phase S2 — the API of `cargo style`, designed for speed

**Repo:** design document, reviewed by dwight. **Depends on:** S1.

1. Start from what a style production asks: "what is the next ticket for this unit", "what changed since the last refill", "which sites does this rule touch". Not from what the Python code calls today.
2. One request per refill. Facts stay inside the process. No caller reads or parses the same file content two times.
3. Name each cost that the API adds: process start, request decode, reply encode, and the cache read.
4. Subcommand names go to nate in the S2 review. Field names are style-unit's decision.

**Milestone 6** when the design is ready for review.

### Phases S3 to S8 — the selection moves into `cargo-style`

**Repo:** cargo-liner, except the Python adapters in `~/.claude`. Re-based from the old phases 7 to 12, with the same proofs and estimates.

Provisional: S2 designs the API from the uses and can change these phases. The S2 review re-plans S3 to S8.

| # | Was | Work | Repo | Depends on |
| --- | --- | --- | --- | --- |
| S3 | 7 | Declaration outcomes, site evidence, rename permission, footprints | cargo-liner | S2 |
| S4 | 8 | Candidate generators, judgment coverage, scope comparisons | cargo-liner | S2 |
| S5 | 9 | Complete selection; Python publication adapter | both | S3, S4 |
| S6 | 10 | Compact cache (`selection.sqlite`), source changes, partition invalidation | both | S5 |
| S7 | 11 | Timing, memory, Mac proof against the S1 targets | both | S6 |
| S8 | 12 | Shadow comparison, switch with nate's go, removal of Python selection | `~/.claude` | S7 |

S3 and S4 run at the same time. Each speed phase reports its result against its target: **milestone 7**.

### `syn` and reuse

1. `syn`: the reader copies the Python tokenizer, because the cutover proof needed equal output. That reason ends when the Python selection is gone (S8) and N1 is settled. Then measure `syn` (already a workspace dependency, 3.0.6) against the tokenizer on the 25 MB cargo-liner corpus, with fact equality. An earlier probe parsed 25 MB with `syn` in 0.22 s on eight threads.
2. `cargo-mend/src/rust_syntax` (about 2,400 lines: parsed sources, module map, identifier rules): evaluate in S3 for the module map and declarations. cargo-mend is a binary crate; use needs a shared library, which comes only with a measured need.
3. `cargo-berth/src/git` (about 12,800 lines): it reads refs, status, worktrees and history. The reader's Git code (197 lines) reads blobs by tree. Evaluate in S4 for history and revision comparison, where the profile shows Git cost (about 12%). Same library rule.
4. A shared library follows the `tui_pane` pattern: a path dependency on main, and the release step pins the published version. A library that cargo-mend uses must be published.

## Milestones (messages to dwight)

1. The draft plan is ready for review.
2. S1: today's times are measured; a target is set for each use.
3. M1: `cargo-style` is on cargo-liner main and installed on natedev and the Mac.
4. M2: the Python client runs the installed `cargo-style`; the build on use is gone.
5. M3: the Rust is out of `~/.claude`; an AWS gate has zero style failures.
6. S2: the API design for speed is ready for review.
7. S3 to S8: each speed step that reaches or misses its target.

Outside the milestones, style-unit writes to dwight only for a decision nate must take, a block, or an ETA change of more than one hour.

## Work order across units (sdlc's call)

1. The main-red fix (done: main 01a2815b0).
2. The phase 6 checkpoint (`~/.claude`).
3. M1, M2, M3, then S1 and S2. S1 can start after the phase 6 checkpoint, beside M1.
4. bf5-style when devtools/bf5 is on main.
5. S3 to S8.

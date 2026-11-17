# Nextest width and lint scope

## What it is

Two policies in the cargo layer that every session's builds go through. First, every `cargo nextest run` that `scripts/lint/invoke.sh` starts names its test width (`--test-threads N`), chosen by one mode line. On a host with a jobserver pool, the run holds one pool token for each test thread. The memory gate pools every width's history for its reservation, and `scripts/buildlog/width_trial.py` scores a switchback trial between one test process per physical core and one per logical core. Width `logical` is the setting: a 72-hour trial found no memory-wait gain at physical width and slower hana runs. Second, `verify.sh lint` uses CI's clippy target scope (`--workspace --all-targets --all-features`). It adds `--keep-going` and can repair multiply-add warnings before its final judgment. It also checks TOML formatting with taplo.

## How it works

### Nextest width (`scripts/lint/invoke.sh`)

`LINT_TEST_WIDTH` sits beside `run_nextest` and takes one of three values:

| Mode | Width |
| --- | --- |
| `trial` | alternates by 100-minute block: `physical` in an even block, `logical` in an odd one |
| `physical` | physical core count |
| `logical` (current) | logical core count: 32 on natedev, nextest's own default there |

- `nextest_physical_cores [sysfs_root]` sets `LINT_PHYSICAL_CORES` once per shell. On Linux it counts distinct `physical_package_id:core_id` pairs under `${1:-/sys/devices/system/cpu}/cpu[0-9]*/topology/`; on darwin it reads `sysctl -n hw.physicalcpu`. A value that is not a positive integer leaves it empty.
- `nextest_logical_cores` sets `LINT_LOGICAL_CORES` once from `getconf _NPROCESSORS_ONLN`, with the same rule.
- `nextest_width_arm EPOCH` sets `LINT_TEST_WIDTH_ARM` to `physical` when `(EPOCH / 6000) % 2 == 0`, else `logical`. `trial` mode passes `${EPOCHSECONDS:-$(date +%s)}`.
- `require_nextest` runs `cargo nextest --version`. On a failure it prints `cargo nextest is required`, then the first error line when there is one, such as a Mac cargo gate refusal. It exits 2.
- `run_nextest` runs `require_nextest`, waits out any build hold (`build_hold_wait_local`), then picks the width and runs `cargo nextest run --test-threads <N> "$@"`. It adds no flag when `NEXTEST_TEST_THREADS` is set, when an argument before `--` is `--test-threads`, `--test-threads=*`, `--jobs`, `--jobs=*`, `-j` or `-j*`, or when the core count is unreadable. Arguments after `--` belong to the test binaries and never suppress the width. The exit status passes through unchanged.

### Jobserver tokens (`scripts/lint/nextest_tokens.py`)

On natedev, compiles and test threads share one jobserver pool, `/dev/steve`. Each token is one byte: a read takes a token, and a write of the same byte gives it back.

1. `run_nextest` calls `nextest_token_pool` in `physical` and `logical` mode. It sets `LINT_NEXTEST_POOL` to the path in `CARGO_MAKEFLAGS` `--jobserver-auth=fifo:PATH` when that path is a readable and writable character device or FIFO. Otherwise the pool is empty.
2. After the memory gate admits the step, `run_step_attempt` runs `nextest_tokens.py --pool P --width-file F --reservation R -- cargo nextest run --test-threads W ...` inside the step's scope.
3. The holder builds first with `--no-run` and the selection arguments before `--`. The compile takes tokens as cargo always does, and the step holds no test token while it compiles.
4. It takes `test_token_floor` tokens (`config/build_admission.conf`, 4), and waits for them with `nextest: waiting for K more jobserver tokens`. `progress_profile.token_wait_line` defines that line; the Builds page shows the wait as the stage `waiting for jobserver tokens`. Then it takes free tokens without a wait, up to W.
5. It writes the token count N to F. When N is not W, it sets the reservation's `test_threads` to N (`admission_client.width`).
6. It prints `nextest: N of W test threads; load <one-minute load> on <cpu count> CPUs (<host>); one jobserver token for each thread`, then runs the tests with `--test-threads N`.
7. Every token goes back when the tests end, on any exit or signal. TERM, INT and HUP go on to the child, and the holder exits 128 + the signal. steve also takes back the tokens of a process that dies.
8. The shell reads F, removes it, and puts N in the step's argv for the build log. The memory sampler recomputes the test-phase context with N when the test phase starts.

- A build failure returns its status and takes no token. A pool that does not open prints `nextest: W test threads with no jobserver tokens: <pool>: <error>` and runs at W.
- `trial` mode and a host with no pool (the Mac) take no tokens. Those runs, and a caller-supplied width, print the line from `nextest_budget.py report`: `nextest: <granted> of <requested> test threads; load <one-minute load> on <cpu count> CPUs (<host>)`. The Python formatter prints `load unknown` if it cannot read the load.
- steve stops handing out tokens while MemAvailable is under 12 GiB, so a holder can wait at the floor on memory too.
- hana CI's nextest runs through `scripts/build_queue/ci_cargo_front.py` with `/dev/steve-ci`, not through `run_once`, and takes no test tokens.

natedev has 16 physical and 32 logical cores. The Mac reads 12 for both and has no memory gate. GitHub CI and hand-run `cargo nextest` do not go through `invoke.sh` and keep nextest's default width.

A step records its width through its argv: `invoke.sh` puts `--test-threads N` before the caller's arguments, so the first width in argv is the one the build log's `steps.argv`, the memory gate's reservation row and the `anon_peaks.jsonl` record all carry. A run that holds fewer tokens than its width records the width it ran at: the build log takes the argv with N, and the holder rewrites the reservation's `test_threads`.

### Test prebuilds (`scripts/lint/prebuild.py`)

A workspace can declare an artifact that its tests need, in its root `Cargo.toml`:

```toml
[workspace.metadata.dev.prebuild.hana_agora_server]
target = "wasm32-unknown-unknown"
env = "HANA_AGORA_SERVER_WASM"
for = ["hana_agora", "hana"]
platforms = ["linux"]
```

`platforms` is optional. It lists the platforms that build the artifact, `linux` and `macos`; the default is both.

1. `run_step_attempt` calls `lint_prebuild_prefix` for a `cargo nextest run` step. When a line of the root manifest holds the word `prebuild`, it puts `prebuild.py --manifest M --` in front of the token holder and the nextest command. The word after a `#` with no quote before it is a comment and does not count. So the check finds a declaration in every TOML form: a header, a quoted or dotted key, or an inline table. The word in a value or another table starts Python once; `prebuild.py` reads no declaration and runs the tests unchanged. The shell reads the manifest without a fork, so a manifest without a declaration starts no process. `verify.sh` sets the manifest (`LINT_WORKSPACE_MANIFEST`) to the repository root `Cargo.toml`. A remote `lint nextest` runs in the mirror root and reads `$PWD/Cargo.toml`.
2. `prebuild.py` reads the packages that the nextest arguments select. Plain `-p`/`--package` names select those packages; a `-E` filter can only narrow them. A glob, `--workspace`/`--all` (cargo then ignores `-p`), a short-option word other than `-p` that holds a `p` (`-phana`, `-rp`) or no `-p` selects the whole workspace. Filtersets are not read: a run that selects by `-E` alone builds every entry for its platform, and cargo's fingerprints make the extra builds cheap.
3. For each entry whose `for` list holds a selected package, or for every entry on a whole-workspace run, it runs `cargo build --release --manifest-path M -p <package> --target <target>`. A `--target-dir` in the nextest arguments goes to the build too.
4. It reads cargo's JSON messages for the `cdylib` or `bin` artifact whose `package_id` names the package, so a renamed `[lib]` target works. It needs exactly one such file, and sets `<env>` to its path. Then it runs the nextest command with `exec`.

- The build runs inside the step's admission and memory scope, on the host that runs the tests: natedev, the Mac or an SSH target.
- On a platform that `platforms` leaves out, it prints `prebuild: <package> builds only on <platforms>; not built on <platform>` and runs the tests without the build.
- Cargo's fingerprints make an unchanged build a no-op. There is no second cache.
- A failed build exits with cargo's status and prints `the tests did not run`. An invalid declaration exits 2 with one line. A `workspace`, `metadata` or `dev` value that is not a table declares nothing, and the tests run unchanged.
- The profile is always `release`. The entry has no profile key.

### Width in the reservation (`scripts/lint/memory_admit.py`)

- `test_threads(argv) -> int | None` returns the first width in argv, from `--test-threads N`, `--test-threads=N`, `-j N` or `-jN`; absent or non-integer (`num-cpus`) is `None`.
- `expected_peak(repo, step, host, now, root, test_threads=None)` reads only history before `now`, so a later call for a past `now` returns the same value. Its measured tier (≥ 5 `anon_peaks.jsonl` records in `HISTORY_DAYS` 14, p90) reads every width when `test_threads` is `None`, and only that width's records when one is given (`width_trial.py`'s R row). Its index tier (build-log `peak_mem_bytes` × `ANON_SHARE` 0.65) and `FALLBACK` (12 GiB) do not look at width.
- `check` calls `expected_peak` with no width, so the gate pools every width: one width's history was thin and rebuild-heavy (11.4 GiB at width 4, 17.2 at 5, against 10.9 pooled, nightly 2026-10-16), and the token width is known only after admission. It writes `test_threads(argv)` and `call_id` (`BUILDLOG_CALL_ID`, else `None`) into each reservation row; `set_width` (`memory_admit.py width <reservation> <n>`, under the admission lock) rewrites `test_threads` after a narrower token count, and `release` copies both into the `anon_peaks.jsonl` record.

### Trial scorecard (`scripts/buildlog/width_trial.py`)

`width_trial.py report --since <ISO-8601 with offset> --until <ISO-8601 | now>` prints the switchback scorecard for natedev. Exit 0 on a report, 2 on a usage error.

- `load_steps` reads `step = 'nextest'` rows from the build-log index (`index_path()`, moved by `BUILDLOG_DIR`) and selects them by request time, `started_at − mem_wait_s`. A step's arm is `test_threads(argv)`: 16 is `physical`, 32 `logical`, none `unset`, any other width its number. Its block is `request // BLOCK_S` (6000 s).
- **whole-hana** run: `caller = 'verify'`, `repo = 'hana'`, the `-E` value is exactly `package(hana)`, and no `--test` in argv.
- Metrics, `physical` against `logical`:

| Metric | Definition | Rule |
| --- | --- | --- |
| W | mean `mem_wait_s` per hana nextest step, any caller; steps requested in a block's first 900 s (`WASHOUT_S`) left out; NULL waits skipped | `proven` at ratio ≤ 0.50 and interval high < 1.00; `partial` at high < 1.00; `none` at high ≥ 1.00 |
| P | p50 anon peak of whole-hana runs, from `anon_peaks.jsonl` joined on `call_id` | target physical ≤ 6.5 GiB, no effect on the verdict |
| R | `expected_peak("hana", "nextest", host, until, root, width)` for 16 and 32 | target physical ≤ 9.0 GiB, no effect on the verdict |
| T | p50 of `duration_s − finished_s` over whole-hana runs with `finished_s` set | fails at ratio > 1.11 and interval low > 1.00 |
| F | share of whole-hana runs with `status ≠ 0`, difference in points | fails at difference > +5 and interval low > 0 |

- Reported without a rule: timed-out tests per 100 whole-hana runs (`tests.status = 'timed_out'`), and mean memory wait per gated step over every natedev step, each block assigned the arm most of its nextest steps ran.
- Intervals: 90% percentile intervals from 2,000 block-bootstrap resamples (each arm's blocks drawn with replacement, same count), `random.Random(20261009)`.
- Sample: ≥ 60 whole-hana runs (`MIN_RUNS`) and ≥ 15 blocks (`MIN_BLOCKS`) per arm, counting only `physical` and `logical` when either is present.
- `verdict(w, t, f)`: W `none` → `revert`; W `proven`/`partial` with T or F failing → `trade-off`; otherwise `keep`. A metric that cannot compare (`UncomparedMetric`, reason `needs both arms`, `zero denominator` or `no resamples`) gives `no verdict`, and the row prints `—` where a value needs the other arm.
- P pairs each attempt of a call with its own anon peak by end time when the counts match; a retried call missing a peak gives no P for any of its attempts.
- The header carries the script's own sha256 (first 12 hex) and the window in the user's zone (`user_zone()`).

### CI-scope lint (`scripts/direct/verify.sh`, `lint` arm)

Order: `require_taplo`, mend `--fix`, nightly fmt of the package and changed members, taplo, clippy. Workspace rustdoc runs separately through `verify.sh doc` at the checkpoint.

- Clippy uses `invoke_clippy --workspace --all-targets --all-features --keep-going`.
  `invoke.sh` adds `-- -D warnings`.
  The target scope matches CI's `clippy` job and `scripts/release/pre_release_checks.sh`.
  The lint callers add `--keep-going`; `invoke.sh` does not add it.
  Lint takes no trailing `--features`.
  `verify.sh doc [<package>] --features <list>` checks the staged workspace.
  A named package owns feature names without a slash.
  CI's Windows-target Clippy pass has no local counterpart.
  The other commands keep their own scope.
- Clippy judges first. A passing judgment needs no fixes. Status 101 permits repairs for lint findings, compile errors, and unknown diagnostics. A kill, a compiler signal report, or a lint hold prevents repairs. Other initial failures return directly. Repairs run `cargo clippy --fix`, then fmt, with the judgment scope and lint levels. Repairs stop when the tree remains unchanged or reaches `clippy_fix_pass_limit` from lint.conf. The default limit is 3. Clippy judges again after repairs. The final judgment sets the lint status. See [Lint-step fix](build-followups-mul-add.md#lint-step-fix).
- Taplo: `run taplo fmt --check` from `workspace_root` (from `cargo metadata`), so taplo loads the workspace `taplo.toml` whatever directory lint starts in. `config/lint.conf` does not gate it; it gates only the cargo stages. An unformatted `.toml` fails lint, names the file, and is cached like any lint failure.
- `require_taplo` exits 2 with `taplo is required` before any stage when taplo is not on `PATH`. A taplo panic (exit 101) exits 3 on macOS, with a message to re-run unsandboxed, and 2 elsewhere.
- `buildlog_step_compiles` in `invoke.sh` returns 1 for `taplo` (as for `cargo [+tc] fmt` and `*/sweep.py`), so taplo skips the build-hold wait and the memory gate.

## Invariants

- `invoke.sh` is the only place nextest policy flags are composed; callers own scope.
- The prebuild prefix goes before the token holder: `nextest_tokens.py` reads `cargo nextest run --test-threads W` at the start of its command.
- The caller's width wins: a width flag before `--`, or `NEXTEST_TEST_THREADS`, means `invoke.sh` adds nothing. Such a run takes no test tokens.
- No counter: a run's tokens are the bytes it holds, and steve takes back the tokens of a dead process.
- Width selection never fails, prints or changes a status; an unreadable core count adds no flag, and a pool that does not open runs at the full width.
- `invoke.sh` adds its width before the caller's arguments, and `parse.test_threads` (admission calls it as `memory_admit.test_threads`) and `width_trial` read the first width in argv. Change both or neither.
- The trial block in `nextest_width_arm` (6000 s) equals `BLOCK_S` in `width_trial.py`.
- `invoke.sh`'s ungated set (`buildlog_step_compiles`) and `width_trial._uses_memory_gate` decide which steps count as gated; they must agree. Both exempt `cargo [+tc] fmt`, `*/sweep.py` and `taplo`, so none of those counts in the all-steps gated-wait row.
- The build log and `anon_peaks.jsonl` are evidence: nothing edits or deletes their records. A width filter replaces any need to drop stale records.
- `verify.sh lint`'s clippy target scope equals CI's `clippy` job and `pre_release_checks.sh`; change all three together. Its fix passes and its judgment all use `--keep-going`.
- Tooling failures, killed runs, and lost-token status 125 are never cached. Only a lint failure of the tree is cached.
- `invoke.sh` must parse under the Mac's bash 3.2 (no `EPOCHSECONDS`, no associative arrays, `${arr[@]+"${arr[@]}"}` for empty arrays).
- Tests never read the live `~/.local/state/buildlog` (`BUILDLOG_DIR` points at a temp dir) and never run real `cargo`. Tests that reach `run_nextest` set `CARGO_MAKEFLAGS` to a FIFO of their own or to a path with no pool, so they never take a live `/dev/steve` token. `test_nextest_width.py` sets the mode in every case, so it passes under any `LINT_TEST_WIDTH` default.

## Calibration / gotchas

- Switching width is one value: `LINT_TEST_WIDTH`. The width functions and the `trial`/`physical` modes stay in place for that.
- `verify.sh` retries a memory-killed step once under the same `BUILDLOG_CALL_ID`, so `call_id` alone does not identify one attempt.
- A step run outside `verify.sh` records `call_id` None; whole-hana runs all come through `verify.sh`.
- `verify.sh`'s pass key hashes content and these scripts: touching a file replays a cached pass, so a timing run passes `--no-cache`, and every pass record misses once after an `invoke.sh` change.
- Under `--all-features` hana pulls `bevy/hotpatching`, so the first lint in a target directory checks the bevy tree a second time (159 s against 128 s on hana main).
- Any `verify.sh` test fixture that runs `lint` needs `workspace_root` in its `cargo metadata` answer and a `taplo` on `PATH`.
- `~/rust/rust-template` is a cargo-generate template with no `Cargo.toml`; a live width check runs on a real crate such as `~/rust/cargo-liner`.
- Interim reads of the scorecard move: T read as failing at 64 h of the 72 h trial and passed at 72 h (low end exactly 1.00).
- Physical width cuts the whole-hana anon peak p50 by about a fifth but does not lower the gate's reservation, which follows the gate's own recorded peaks.

## Why

**Width stays `logical`.** The user approved one test process per physical core (16 on natedev) on condition that a measurement prove or disprove it. The trial ran 2026-10-09 18:05 – 2026-10-12 18:05 PDT, with every threshold fixed before it started and the scorecard's hash checked at the readout:

| Metric | physical | logical | ratio | 90% interval | result |
| --- | --- | --- | --- | --- | --- |
| W memory wait per hana test step (s) | 19.2 | 18.2 | 1.05 | 0.53–2.27 | none |
| P whole-hana anon peak p50 (GiB) | 7.06 | 8.69 | 0.81 | 0.75–0.91 | unmet |
| R gate reservation (GiB) | 9.80 | 9.70 | — | — | unmet |
| T whole-hana exec p50 (s) | 372 | 330 | 1.13 | 1.00–1.21 | pass |
| F whole-hana failed runs (%) | 38.6 | 45.6 | −7.0 pt | −17.9–5.1 pt | pass |

Sample: 22 blocks per arm; 114 and 103 whole-hana runs. Verdict `revert`: W shows no memory-wait gain, and whole-hana runs are 13% slower at 16 threads. The widths stay labeled so a later measure can read them.

The per-width key replaced deleting hana's old anon-peak records. Since 2026-10-20 the gate pools the widths again, and the labels stay for `width_trial.py`.

**Lint runs CI's clippy.** A narrower local clippy (`--lib --bins --tests`, default features) passed on trees CI's Clippy job failed, for example a missing `#[must_use]` in hana_diegetic's `bench_support`. Measured on hana, `verify.sh lint hana --no-cache`, clippy step, memory wait 0 in every run:

| Run | Narrow scope (s) | CI scope (s) | Anon peak (GiB) |
| --- | --- | --- | --- |
| first lint | 127.8 | 159.2 | 4.00 → 3.98 |
| touched file | 15.2 | 52.8 | 2.84 → 2.77 |

The touched-file lint is 3.5× slower (37 s more), with no higher anon peak. That cost was accepted on 2026-10-13: one lint that fails where CI fails is simpler than two lints of which one passes where CI fails, and a CI round trip costs far more than 37 s per lint. Taplo joins the same arm, run as the push gate runs it, so TOML formatting fails locally too.

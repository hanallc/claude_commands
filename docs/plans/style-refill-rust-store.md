# Refill on a fast store: design brief

Status: design (style-unit). The brief is from 2026-11-14; the design below it is from 2026-11-15.

## The ask

The user, 2026-11-14: "i am wondering if potentially we could use a local spacetime instance - it is supposed to be
pretty fast - faster than other db solutions - and a rsut program to run it - the current times are poor - please ask
an astra designer to help design a solution with you - one that we can build in parallel because i don't want to lose
our current work as it needs to continue to work until we have a fast replacement".

Design a refill that is fast on a cold start and on a warm start. Evaluate the user's idea first: a local SpacetimeDB
instance and a Rust program that runs the refill. If SpacetimeDB is not the best fit, say why, and name the store
that is. Compare on cold time, warm time, memory, and how simple the result is to build and debug.

## Current numbers

Measured on natedev under `systemd-run --user --scope -p MemoryMax=2G -p MemorySwapMax=0`, cargo-liner ledger:

| Run | Time | Peak memory |
| --- | --- | --- |
| Cold refill (no saved results) | 15:46 | 1.70 GB |
| Warm refill (saved results) | 0:37 | 0.62 GB |
| Cold refill before phase 5 of the refill plan | 19:18 | 1.73 GB |

The ledger is one SQLite file of about 3.7 GB (`~/.local/state/style/cargo-liner/ledger.sqlite`).

After the row and tokenize cuts (branch `sdlc-style-cold-refill-rows`, 2026-11-14), on a copy of the live ledger:

| Measure | Before | After |
| --- | --- | --- |
| Dependency rows | 4,698,263 | 451,462 |
| Ledger size | 4.39 GB | 2.00 GB |
| Cold refill | 12:32 | 9:54 |
| Bytes written, cold | 63.5 GB | 26.6 GB |
| Warm refill | 0:34 | 0:29 |
| Cold peak memory | 1.92 GB | 1.90 GB |

Cell rows (branch `sdlc-style-cell-columns`): dependency rows 451,143 -> 168,053 and ledger 1.99 -> 1.79 GB. Cold and
warm time did not change (measured side by side under the same load: cold 19:07 -> 18:56, warm 0:58 -> 1:01).

Module renames once (branch `sdlc-style-module-renames-once`), side by side under the same load: `_module_renames`
tokenize calls 4,426 -> 1,912 (205 M -> 59 M characters), cold 16:24 -> 13:14, bytes written 26.0 -> 5.3 GB, warm
0:44 -> 0:42, cold peak 1.91 -> 1.92 GB. Both sides gave the same result.

WAL journal mode gave no cut (cold 10:24, 27.9 GB written), so the ledger stays in rollback mode.

## Current system

- Code: `scripts/style/style_plan.py` (refill entry, selection), `scripts/style/style_ledger.py` (ledger, coverage,
  yields), `scripts/style/refill_findings.py`, `refill_cells.py`, `refill_facts.py`, `refill_inputs.py`,
  `style_footprint.py`, `declarations.py`, `finding_sites.py`, `approved_renames.py`.
- As-built: `docs/as-built/style-showrunner.md`.
- Earlier speed plan: `docs/plans/style-refill-in-seconds.md` (phases 1-5 landed; phase 6, refill groups and outputs,
  is not started). Its target was a few seconds and less than 200 MB for an ordinary refill.
- Inputs: Git objects of the target repository (cargo-liner), the ledger tables, rule files in
  `~/rust/nate_style/rust/`, and the staff and plan files.

## Early profile of the cold path

The first 534 one-second samples of the cold run:

| Work | Share | Cause |
| --- | --- | --- |
| Declaration reading (`_declaration_reading`, `_compute_declaration_groups`) | about 35% | Python tokenizer and parser per file |
| Footprints (`finding_footprint`, `module_readings`, `_module_renames`) | about 45% | Python tokenizer per file, read again per finding |
| Saved-result writes (`refill_facts.dependencies`, `input_fact`, `publication`) | about 20% | Point reads and writes on a 3.7 GB SQLite file under a 2 GB limit |

Full profile of the cold run with the row cut (one-second samples): `input_fact` about 15% (leaf),
`run_git` about 14%, `token_spans` and `tokenized_text` about 18%.

Tokenizer calls in one cold run, after the cuts:

| Caller | Calls | Distinct texts | Characters |
| --- | --: | --: | --: |
| `_module_renames` (via `module_readings`) | 4,629 | 926 | 214 M |
| `declarations.source_spans` | 4,625 | 3,141 | 121 M |
| `module_declaration_text` | 1,712 | 1,712 | 38 M |
| `_rust_code` | 1,094 | 1,094 | 28 M |

`_module_renames` reads each large file about 5 times, once for each moved file. A token cache for those files does
not fit under the 2 GB limit. A reader keyed by blob id removes this repeat.

The user, 2026-11-14: "wouldn't the other parts go faster if it was syn based - and running in parallel on multiple
threads?" Evaluate a Rust reader that parses each blob once with `syn`, on many threads (for example `rayon`), keyed
by Git blob id, so a blob is parsed once for all rules and findings.

## Constraints

1. The current Python refill keeps working, unchanged in behavior, until the replacement proves the same output and
   is faster. Build the replacement beside it.
2. The phases must be able to run in parallel with the current style work.
3. Each phase proves its output against the current refill on the same inputs (a differential run).
4. A heavy run on natedev runs only under `MemoryMax=2G`. Nothing runs on the Mac outside the build queue.
5. The live ledger opens read-only. Probes use a copy.

## What the design gives

1. The architecture: the store, the Rust program, and how they read Git and the ledger.
2. The data model, and how a change in an input finds the results it changes.
3. The cutover: shadow runs, the differential proof, and the switch.
4. Phases that can run beside the current work, each with its proof and an estimate.
5. Risks, and what to measure first to settle the store choice.

## Design

Status: design (style-unit, 2026-11-15). Written without a designer, under no-worker mode, at the showrunner's request.

### Result

Do not use SpacetimeDB. Keep SQLite as the ledger. Build a Rust reader beside the Python refill.

The reader reads Git objects in its own process. It reads each Rust blob once, on all CPUs, and returns the
facts that the refill now gets from the Python tokenizer. A blob fact is keyed by the blob id and the reader
version. It never goes stale, so it needs no dependency row and no saved result.

The store is not the main cost. The cold refill spends its time in Python token reads, Git processes and
saved-result reads and writes. A faster store changes only the last part. A Rust reader removes the first two
parts, and with them most of the saved results.

Expected times on cargo-liner, cold and warm, under the same load as the measure:

| Point | Cold | Warm |
| --- | --: | --: |
| Now (2026-11-14 measure) | 13:14 | 0:42 |
| After phase 4 (reader and no saved blob facts) | below 4:00 | below 0:20 |
| After phase 5 stages (selection ported where the profile names it) | below 0:10 | below 0:03 |

The phase 4 number is an estimate: the token reads, `run_git` and `input_fact` are about half of the cold
profile, and the Python code that walks token tuples is a further part. Phase 4 measures it. Seconds, which the
user asks for, need phase 5: the selection code is Python too.

### Why the cold refill is slow

The target repository is small. At `cargo-liner-style` `ff68c6bc4`, the tip has 1,318 Rust files with 25.0 MB of
text. A cold refill reads about 3,141 distinct Rust texts across the commits that it compares.

The Python refill tokenizes about 250 M characters in one cold run: each byte about ten times.

| Caller | Calls | Distinct texts | Characters |
| --- | --: | --: | --: |
| `declarations.source_spans` | 4,625 | 3,141 | 121 M |
| `_head_spans` (module renames) | 1,912 | 923 | 59 M |
| `module_declaration_text` | 1,712 | 1,712 | 38 M |
| `_rust_code` | 1,094 | 1,094 | 28 M |

The cold profile after the row cuts: token reads about 18%, `run_git` about 14%, `input_fact` about 15%.
Most other samples are Python code that reads the token tuples that these calls make.
Saved results exist because the Python reads are slow; they write 5.3 GB in a cold run.

A Rust probe (natedev, 2026-11-15, load 22) read the 1,306 Rust files of the `cargo-liner-style` work tree
(25.0 MB) and lexed and parsed each one:

| Threads | Read | Lex (`proc-macro2`) | Parse (`syn` 2, `full`) | Peak memory |
| --: | --: | --: | --: | --: |
| 1 | 0.014 s | 0.26 s | 1.26 s | 68 MB |
| 8 | 0.009 s | 0.041 s | 0.22 s | 138 MB |
| 32 | 0.007 s | 0.036 s | 0.17 s | 251 MB |

`syn` parsed all 1,306 files. A cold refill reads about 2.4 times this text (3,141 distinct texts), so one read of
each blob costs less than one second on 32 threads, and about 3 seconds on one thread. The Python refill spends
minutes on the same reads.

### Store choice

| Need | SpacetimeDB | SQLite (current) | Embedded key-value (redb) | No store for blob facts |
| --- | --- | --- | --- | --- |
| Python readers (refill, Units report, `rules.py`, tests) | No Python client; every reader changes | `sqlite3` in the standard library | Needs a Rust bridge | Not a reader |
| Process count | A server that must run before each refill | None | None | None |
| Memory | All tables in RAM; the ledger is 1.79 GB, near the 2 GB limit | Page cache only | Page cache only | None |
| Where the parse runs | Outside it: reducers run WebAssembly with no file or Git access, under one write lock | Outside it | Outside it | In the reader |
| Fit | Many live clients that subscribe to changes | One batch job, one machine | Fast immutable key reads | Facts cheaper to make than to read |
| Cold and warm time | No cut: the parse and Git reads stay outside it; a network hop is added | As now | Faster key reads only | Cold and warm both lose the saved-result reads and writes |
| License | Business Source License 1.1 | Public domain | MIT or Apache-2.0 | — |

SpacetimeDB is fast for its case: many clients that read and change shared state in memory. The refill is one
batch job on one machine. Its cost is CPU work on Git blobs, which SpacetimeDB cannot run in a reducer. It would
add a server, hold the full ledger in RAM, and need a new client in every Python reader.

Blob facts need no store if the reader makes them in less time than a store read takes. Phase 1 measures this.
If a store is needed, use one SQLite table keyed by blob id, in a separate file next to the ledger. It needs no
dependency rows, because a blob id names its content.

### Architecture

1. `style-reader` (name TBD): one Rust binary in a new folder `rust/style_reader/` of this repository.
   - Git: `gix` reads trees and blobs in its process. No `git` subprocess for a tree list, a blob or a name search.
   - Threads: `rayon` runs one blob on each task.
   - Input: one request on stdin: the repository, the commits, and the fact kinds.
   - Output: JSON lines on stdout, one line per blob and fact kind. Python reads them with `json`.
   - Scope: every Rust blob at the requested commits. A full read costs less than a second, so the client does
     not predict which blobs selection will ask for.
2. `scripts/style/style_reader.py` (name TBD): the Python client. It starts the reader once for each refill
   process and fills the refill's existing in-memory maps. Selection code does not change.
3. A configuration key `[refill] reader = python | rust | shadow`. The default stays `python` until the cutover.
   `shadow` runs both and writes each difference to the refill log.

The reader ports the facts, not the tokens. Token tuples for 250 M characters cannot cross a pipe at a useful
speed. Each fact kind is the output that a Python caller keeps today:

| Fact kind | Python source | Used by |
| --- | --- | --- |
| `declarations` | `declarations.read_declarations` | Declaration groups, name sites, moved-site lines |
| `module_scan` | `DeclarationScan` (module declarations, scopes, `#[path]`, inline modules, item starts, plain words) | `declares_module`, `EditedFiles.declaring` |
| `module_sites` | `_module_renames` sites for every `mod` name in the file | `module_site_readings`, `renamed_text` |
| `name_sites` | `declarations.name_sites` | Approved renames, finding sites |
| `lines` | Line count and line starts | `read_slices`, `fill_decision_batch` |
| `names` | The set of identifiers in the blob | `files_naming` (replaces `git grep`) |

#### syn or a port of the token reader

Speed does not decide this: `syn` parses the full tip in 0.17 s on 32 threads. Equal output decides it.

The Python facts are defined on its own token stream: comments drop out, literals stay opaque, and an
unterminated comment or an unknown character makes a file unreadable. `syn` builds a full syntax tree with its
own rules. It rejects a file that is not valid Rust, for example an old commit in the middle of an edit, where
the Python stream still reads the `mod` lines. It also reads some text in a different way, for example a raw
string or a lifetime. A `syn` reader would change refill results at these edges, and each edge would need a
decision.

Port `tokenized_text` and each fact reader one to one, so the differential proof can require equal output on
every blob. A lexer port costs about the same as the `proc-macro2` lex above. Use `syn` later for a new fact
that needs full syntax, with its own differential case.

Two port details need tests:

1. Python's `str.isspace`, `\d` and `\w` use Unicode classes. Rust's `char::is_whitespace` does not match
   `str.isspace` for U+001C to U+001F. Port the Python classes as tables.
2. Python offsets count code points; Rust offsets count bytes. The reader returns code-point offsets.

### Data model and change detection

| Fact | Key | Lifetime | Dependency rows |
| --- | --- | --- | --- |
| Blob fact | Blob id, fact kind, reader version | Never stale | None |
| Tree list | Commit id | Never stale | None |
| Name search | Commit id, name | Never stale | None (the reader answers it from `names`) |
| Footprint, declaration group, cell state | Current keys in the ledger | As now | Only ledger rows and the commit set |

A change in a Rust file is a new blob id, so it finds no old fact. A change in a ledger row finds its results
through the existing ledger dependency rows. The `git_object` source kind goes away from `refill_dependencies`,
because a result no longer records each blob that it read: it records the commits, and each commit names its
blobs.

When blob facts are fast, a footprint is cheaper to make again than to load. Phase 4 measures each saved
result kind against a new calculation and deletes the saved results that lose.

### Cutover

1. Each phase adds a differential test to the targeted suite. The test runs the Python reader and the Rust reader
   on a fixture corpus and on every blob of a pinned `cargo-liner` commit, and requires equal output.
   A Python change to a ported reader fails this test until the Rust reader changes too.
2. Shadow: the cargo-liner units run `reader = shadow` for one day of refills. Each difference stops the cutover.
3. Switch the default to `rust`. Keep the Python reader for one more day as the `python` value.
4. Delete the Python fact readers, their saved-result kinds and the `shadow` value.

The current refill keeps working at each step: the switch default is `python` until step 3.

### Phases

Each phase runs beside the current style work. It adds new files, and it changes refill code only behind the
switch. Estimates are for one director with no workers.

| # | Phase | Proof | Estimate |
| --- | --- | --- | --- |
| 1 | Reader start: `gix` blob reads, the token reader port, `lines` and `names`, on all CPUs | Equal tokens on every blob of the pinned commit; reader time and peak for all blobs of a cold refill | 4 h |
| 2 | Fact ports: `module_scan`, `module_sites`, `declarations`, `name_sites` | Differential test per kind, fixtures and pinned commit | 6 h |
| 3 | Refill client and switch; `files_naming` and tree lists from the reader | Cold and warm refill, both sides at the same load: same result | 4 h |
| 4 | Remove saved blob facts and `git_object` dependency rows on the `rust` path; measure each saved result kind | Same result; cold, warm, peak, rows, bytes written | 3 h |
| 5 | Profile again; port the next stage the profile names (likely footprints or declaration groups) | Its own differential test | 4 h for each stage |
| 6 | Shadow, switch, delete | One day of shadow refills with no difference | 2 h of work, 2 days of time |

### Risks

1. Two readers exist until phase 6. Other units change the Python readers often (for example
   `edits_module_owners` on 2026-11-14). The differential test makes each such change also change the Rust
   reader. Keep the overlap short.
2. The Mac. A style refill that runs on the Mac needs the reader binary there. The binary must come from the Mac
   build queue, never from natedev. Until it does, the Mac keeps `reader = python`.
3. The build. The reader is a compiled binary in a repository of scripts. On natedev a `cargo build` waits in
   the build queue for memory (the probe waited 32 minutes). Recommendation: the land worker builds the reader
   through the build queue when a land changes `rust/style_reader/`, and the client runs the binary from one
   fixed path. This choice goes to the showrunner before phase 3.
4. The rest of the refill stays Python. If phase 4 leaves the cold refill above one minute, the cost is in
   selection code, and phase 5 ports it a stage at a time.

### What to measure first

1. The reader time and peak memory for all blobs of one cold refill, on 32 threads and on 1 thread.
2. The time to read one blob fact from SQLite against the time to make it again. This settles whether blob
   facts need a store.
3. The cold refill time with the reader against the Python refill, both sides at the same load.

# cargo style: the API, designed for speed (S2)

> **Status:** design for review (milestone 6). Parent plan: `docs/plans/cargo-style.md`, Phase S2.
> Review: dwight, 2026-11-16 (`~/rust/hanadocs/dwight/cargo-style-api-review-2026-11-16.md`). All six points are answered below; dwight accepted the answers.
> Inputs: the S1 result (times, floors, targets) and the S2 inventory of the production's questions, at main `8abb0f0b5`.

## What the API must fix

S1 found three costs. Each one comes from the shape of today's interface, not from the work.

| Cost today (one commit) | Cause in the interface | Fix in this API |
| --- | --- | --- |
| Reader facts, 42 s for 4,637 blobs (one changed) | `Ledger._tip` asks for every `.rs` blob of each new revision, ignored paths too. Facts last only for one ledger instance. Each request asks for all five fact families. | Facts persist by blob id inside `cargo style`. A blob is read and parsed one time per reader version, ever. Facts never cross the process boundary. |
| Python selection, 88 s | Selection runs in Python over full facts and full proofs. | Selection runs inside `cargo style` (S3 to S5). Python gets the answer, not the inputs. |
| Cache read, 37.6 s against a 0.41 s scan | Each read rebuilds full proofs (`decode_proof`, `decode_enumeration`, `JudgmentCells.result`), when a COUNT needs only counts and reasons. | The cache keeps a summary row beside each proof. An answer reads the summaries of its scope and reads a proof only for a selected ticket. |

The inventory also found twelve paths that read or parse the same content again in one refill. Examples: 92 rule-catalog loads, three manifest readers, three source-text readers, and a second ignore-file read through `git show`. In this API, each input has one reader and one cache key (see "One read of each input").

## Rules of the design

1. Start from the production's questions. The inventory lists 25. Five of them carry all of the selection cost; this API answers those five.
2. One request per refill. `next` answers the whole refill: observation, COUNT, the capacity loop and the selected tickets. No second call follows for facts, cells or proofs.
3. Facts stay inside the process. A reply carries answers and the data that writers need. It never carries tokens, spans, masks or candidate text.
4. Each answer that can be absent for more than one reason is an enum. A missing object, unreadable text, an absent declaration, an ambiguous site and missing configuration stay distinct states.
5. No subcommand without a current consumer. A question that only Python commands ask today gets no subcommand until its command moves (the API-surface rule).

## The questions and their answers

| Production question (inventory) | Today | In `cargo style` |
| --- | --- | --- |
| Should this unit resume, repair, select work or wait? | `next_phase`, style_plan.py:3979 | `next` |
| How many approved findings can this unit build? (COUNT) | `count_refill`, style_plan.py:3751 | `next` (inside the capacity loop); `next --count` for the count alone |
| What did main touch since the synced tip? | `main_changes`, style_plan.py:201 | `next` (reply field `changed`) |
| Does the next ticket need sync or a preceding merge? | `ticket_sync_wait`, style_plan.py:262 | `next` (outcome `SyncWait` or `MergeWait`) |
| Which files are unavailable? | `busy_files`, style_plan.py:412 | input to `next` (see "The request") |
| Which tickets can this unit take, in order? | `candidate_tickets`, style_plan.py:2125 | `queue` |
| Which rule/file cells need judgment? How much coverage remains? | `cells`, style_ledger.py:9339; `prepare_report`, coverage_report.py:177; `read_snapshot`, style_units.py:122 | `coverage` |
| Which sites does this rule touch? | `_refresh_candidate_sites`, style_ledger.py:8612 | `sites` |
| Where is a finding's declaration now? Which files can it change? | `declaration_reading`, style_ledger.py:8161; `EditedFiles.footprint`, style_plan.py:2806 | `finding` |
| Rule catalog, setup, gate, verify, checkpoint, sync and resume, lint, bulk, proposals, split | Python commands | Stay in Python through S8. They call `finding`, `sites` or `coverage` where they need facts today. |

Five subcommands. nate approved the names (see "For nate"). Field and type names are style-unit's call.

## The request

A machine call and a person use the same subcommand. Flags name the question. One JSON file carries production state that only `~/.claude` holds.

```
cargo style next --repo <path> --ledger <style ledger> --unit <unit> --state <state.json> [--count]
```

1. `--ledger`: `cargo style` opens the Python ledger read-only for tickets, findings, approvals, judgments and sync. The Python publication adapter stays the only writer of the ledger until S8.
2. `--state`: the reservation board, other units' dirty paths, open widened paths, capacity and plan state. These come from `~/.claude` tools, so the adapter passes them in. The file is small: paths and counts.
3. `cargo style` owns its own cache, `selection.sqlite` (S6). It holds facts, membership and derived summaries. Only `cargo style` writes it. "Where the cache lives" gives its place and the rule for two writers.

## The reply

JSON on stdout, one object, schema-versioned. The top level is the outcome:

```rust
enum NextOutcome {
    Selected { tickets: Vec<SelectedTicket> }, // up to capacity, in order
    Resume { ticket: TicketId },
    Repair { ticket: TicketId, reason: RepairReason },
    NoWork { reasons: Vec<FamilyReason> },
    SyncWait { tip: Revision, needs: Vec<PathBuf> },
    MergeWait { predecessor: TicketId },
    StructureWait { findings: Vec<FindingId> },
    Unavailable { prerequisite: Prerequisite },
}
```

Each `SelectedTicket` carries what `ticket_add` (style_ledger.py:3586) and `write_phase` (style_plan.py:2643) need, so no writer reads facts again:

1. Kind, family, crate, ordered files, rule ids and exact rule pins.
2. Sites, explicit cells, finding ids, proposal paths and the base commit.
3. Phase-writing data: pinned rule bytes, required paths, read windows, crate target kind, waived findings and review scope.

Every reply also carries `observed`: the main tip, the unit tip, the rule-pin set and the ledger version it read. The adapter uses it to refuse a stale write, as `PendingPublication` (style_plan.py:1386) does today.

The other four subcommands use the same envelope. `queue` returns ordered tickets with a reason for each exclusion. `coverage` returns counts by rule, family and state, with an optional crate scope. `sites` returns file, line and site id for one rule. `finding` returns the current location as an enum (`Found`, `Moved`, `Removed`, `Ambiguous`) and the footprint.

## One read of each input

| Input | Cache key | Read |
| --- | --- | --- |
| Rust blob facts (tokens, declarations, names, modules, code identity) | blob id + reader version | One time ever. Blobs are immutable, so the entry never goes stale. |
| Tree membership, ignores, manifests | tree id | One time per tree. One manifest reader and one ignore reader. |
| Rule catalog and rule metadata | rule pin set | One time per request. |
| Decision files | path + blob id | One time per request. |
| Derived answers (cells, candidates, declaration outcomes, footprints) | the ids of their inputs + program version | Summary row read for counts; proof read only for a selected ticket. |
| A whole `next` answer | see "The key of a stored answer" | A refill with no change returns the stored answer. |

Facts are only prepared for paths the answer needs. Ignored paths and historical revisions get facts only when a proof names them.

## The key of a stored answer

A wrong stored answer is worse than a slow answer. So the key holds every input of the answer, and nothing in the answer comes from outside the key.

| Key part | What it covers | Who changes it |
| --- | --- | --- |
| Main tip, unit tip | All repository source. `next` reads source only from Git trees at these commits, never from a worktree. | Git |
| Rule pin set | Rule text and metadata | the rule catalog |
| Ledger version | Tickets, findings, approvals, judgments, sync, reservations held in the ledger | SQLite triggers (below) |
| Decision digest | The decision files `next` reads (approval and open state). They live in hanadocs and can have open changes, so the key holds a hash of their bytes, not a commit. | the decision writers |
| State digest | The `--state` file: board reservations and other units' dirty paths | the adapter |
| Reader version | Fact output. A hash of the fact source files, made at build time. | the build |
| Program version | Selection code and reply schema. A hash of the whole `cargo-style` source, made at build time. The Python reader client checks the installed crate version and protocol. | the build |
| Unit | The unit asked about | the request |

Facts are keyed by the reader version only, so a selection change keeps every fact. Derived rows and stored answers use the program version.

The ledger version: one row in a new ledger table, `ledger_revision`. A trigger on each table that `next` reads adds one to it on each insert, update and delete. A trigger is in the schema, so every writer changes the version, and no code path can forget it. `PRAGMA data_version` does not serve: it is local to one connection and is not stored.

Decision files are read every request (they are small). Their hash costs one read, which the request does anyway. The worktree is never read, so open changes in a worktree cannot make an answer stale.

## Where the cache lives

One `selection.sqlite` for each repository on each machine, next to the ledger: `~/.local/state/style/<main repository name>/selection.sqlite` (the ledger is `ledger.sqlite` in the same folder, from `ledger_location`, style_ledger.py:9399). All worktrees and all units of one repository share it. Blob ids are equal in each worktree, so a fact one unit builds serves every other unit. A cache for each worktree would pay the cold cost for each unit.

Two processes that write at the same time:

1. The database uses WAL mode. A reader never waits for a writer.
2. Each row is a pure function of its key. Two processes that build the same key build equal rows. Each write is `INSERT OR IGNORE`, so the second write changes nothing.
3. A process writes its new rows in one short transaction at the end of its request, with a busy timeout. No process holds a lock while it parses or selects.
4. Two units that miss the same facts at the same time both build them. This duplicate work happens only on a cold or new blob. It needs no lock and gives no wrong row.
5. A stored `next` answer has the unit in its key, so one unit never reads the answer of another.

## The cost of each call

A unit waits for the full call: the Python adapter, then `cargo style`, then the adapter's writes. The targets below are for the full call, measured at the caller.

The Python adapter, measured 2026-11-16 on a new capture of cargo-liner-style (main `3fba2f055`, unit `6f2f75038`), three runs, median. Evidence: `/session/style/s1-measure/.s2/results.json`.

| Adapter cost | Today | In this design |
| --- | ---: | --- |
| Python start with today's `style_plan.py` imports | 1.02 s | not used: the adapter imports only `json`, `subprocess`, `sqlite3`, `pathlib` and the board reader |
| Python start, thin adapter | 0.032 s | kept |
| Reservation board read (the board command) | 0.236 s | kept; goes into `--state` |
| Other units' dirty paths (`unit_file_holders`) | 0.116 s | kept; goes into `--state` |
| Open widened paths (`pipeline_footprint` per open ticket) | 0.327 s | moves into `cargo style`: it is a ledger computation, not production state |
| `ticket_add` and `ticket_assign` | 0.051 s | kept |
| `write_phase` | 1.324 s | most of it moves: the reply carries the phase-writing data, so the adapter only writes the file. Target under 0.1 s, measured in S5. |

The `cargo style` side, for the refill after one commit:

| Cost | Size | Expected |
| --- | --- | --- |
| Process start | One Rust process per request; no server | about 0.01 s (S1) |
| Request decode | Flags plus a state file of paths and counts | under 1 ms |
| Ledger read | Read-only SQLite; the rows of one unit and its open findings | to measure in S3; bounded by open work, not repository size |
| Cache read | Summary rows of the changed partitions | 0.41 s for a full scan today; S6 partitions it |
| Facts | The changed blobs only | 0.004 s for one blob (S1) |
| Git comparison | `diff` of the stored tip and the new tip | 0.001 s (S1) |
| Selection | Native | not known; S3 to S5 measure it (stop point below) |
| Reply encode | The answer only: tickets up to capacity | KB, under 1 ms |

Targets for the full call:

| Use | Adapter part | `cargo style` part | Full call target |
| --- | ---: | --- | ---: |
| Refill with no change | 0.38 s (start, board, dirty paths) | stored answer: about 0.02 s | under 1 s |
| Refill after one commit, no ticket written | 0.38 s | about 0.43 s floor plus native selection | under 3 s |
| Ticket assignment (one ticket selected and written) | 0.38 s + 0.05 s + `write_phase` under 0.1 s | as the row above | under 3 s |
| Cold refill | 0.38 s | all facts, on all cores (see S3) | under 10 s (provisional) |

So native selection has about 2 s of the 3 s targets. The board read (0.236 s) is the largest adapter cost that stays; it is a separate board process, and a faster board read is the next adapter step if a target is close.

No server process. A Rust start costs 0.01 s, so a server buys almost nothing and adds a lifecycle to manage.

## Effect on S3 to S8

The phases keep their order. Three changes:

1. S3 also builds the fact cache by blob id and the `next` request and reply types. The fact cache is a prerequisite of native declaration outcomes.
   1. Cold (review point 4): 4,637 blobs at 0.004 s is about 18.5 s in series, above the 10 s target. S3 parses on all cores (eight threads give about 2.3 s) and builds facts only for the paths an answer needs. S3 reports the count of needed blobs and the cold time. If cold is still above 10 s, the target comes back for review.
   2. Stop point (review point 5): when the first native selection has a time, S3 compares it with its share of the 3 s target. If it is above that share, the design comes back for review then, not at S7.
2. S5 replaces `next_phase`, `count_refill` and `candidate_tickets` with one `next` call from the Python adapter. `queue`, `coverage`, `sites` and `finding` arrive in the phase that builds their answer: `sites` and `coverage` in S4, `finding` in S3.
3. S6 adds the summary rows and the stored `next` answer to `selection.sqlite`.

## Python commands that still need raw facts (review point 6)

The five answers do not carry everything. A measured inventory (`/session/style/s1-measure/.s2/raw_fact_users.md`) found these Python commands that use raw facts (tokens, spans, bindings, module routes) and stay in Python through S8:

| Command | Raw facts it uses | Covered by an answer? |
| --- | --- | --- |
| Gate (`style_plan.py:559`, `:1053`, `:1580`) | tokens, spans, module sites, binding alignment | No: it needs before and after edit proofs |
| Bulk names and values (`bulk_rename.py`, `bulk_constants.py`) | spans, bindings, module routes, literal tokens | No: execution computes and checks edits |
| Crate runs (`crate_run.py:189`, `:209`) | words, spans, import contexts, loader closures | No |
| Verify, checkpoint, sync and resume, proposals, split | footprints, applied-change proofs, declaration proofs | Partly |
| Lint | none | not needed |

The path until each command moves: these commands keep the request protocol that the installed `cargo-style` serves today (M2). That protocol reads the same fact cache by blob id as `next`. So there is one reader and one copy of each fact; only the transfer to Python stays. No second reader stays by accident. An answer that covers a command fully (for example a conservative footprint for verify) replaces its raw-fact use in the phase that builds that answer.

## For nate

Subcommand names. nate approved these five on 2026-11-16 (review page: "Approve the five names: the style unit builds them as written."):

1. `cargo style next` — what this unit does next.
2. `cargo style queue` — which tickets this unit can take, in order.
3. `cargo style coverage` — how much judgment work remains.
4. `cargo style sites` — which sites a rule touches.
5. `cargo style finding` — where a finding is now and which files it can change.

## Self-made calls

1. One process per request, no server. Risk: if the ledger read is slow at start, a server comes back as an option in S7.
2. `cargo style` reads the Python ledger read-only, and Python stays its only writer until S8. Risk: the ledger schema becomes an interface; a schema change needs both sides.
3. "What changed" is a reply field of `next`, not a subcommand, because no command asks it alone. One-line change if a consumer appears.

# Style refill in Rust

Status: 2026-11-16. Phases 1–6 are complete. Phases 7–12 continue as phases S3–S8 of docs/plans/cargo-style.md, after its move and API phases.

The showrunner selected Rust for selection. SQLite remains the ledger. The user controls the switch to Rust.

## The ask

The user, 2026-11-14: "i am wondering if potentially we could use a local spacetime instance - it is supposed to be
pretty fast - faster than other db solutions - and a rsut program to run it - the current times are poor - please ask
an astra designer to help design a solution with you - one that we can build in parallel because i don't want to lose
our current work as it needs to continue to work until we have a fast replacement".

Design a refill that is fast on a cold start and on a warm start. Evaluate the user's idea first: a local SpacetimeDB
instance and a Rust program that runs the refill. If SpacetimeDB is not the best fit, say why, and name the store
that is. Compare on cold time, warm time, memory, and how simple the result is to build and debug.

Later, the user: "a refill shouldn't even take minutes - you have the wrong design". The showrunner then set the direction: the selection moves to Rust.

## Result

Move the complete selection calculation into the existing `style_reader` package.
Keep Git facts inside Rust throughout selection.
Read authoritative SQLite rows in batches, then calculate with indexed Rust values.
Save compact selection partitions in a separate SQLite cache.
Keep the Python command line and all authoritative writes.

The acceptance targets are below 10 seconds cold and below 3 seconds warm on cargo-liner-style.
The complete process tree must fit below 2 GiB.
These targets are not measured results from a Rust selector.

The current refill remains available throughout construction.
The replacement keeps existing ticket and judgment formats.

## Evidence and limits

### Completed work

The user requested a replacement that permits current work to continue on 2026-11-14.
The user later rejected refill times measured in minutes.

The earlier design predicted cold time below four minutes after phase 4.
The measured result was 494 seconds, or 8:14.
That prediction underestimated Python selection and the cost of transferring facts from Rust.

| Completed phase | Result |
| --- | --- |
| 1 | `gix` reads, the Python token reader port, line facts, and name facts |
| 2 | Module scans, module sites, declarations, name sites, and code identities |
| 3 | Python client, tree inventories, and `reader=python\|rust` |
| 4 | Rust blob facts remain in memory; the Rust path omits several saved blob kinds |
| 5 | Module declarations use `module_scan` once per blob; crate-root checks share results |

The switch in `config/style_reader.conf` defaults to Python.
It selects a fact reader, not a complete selector.
The previous phase 6, “shadow, switch, delete,” becomes phase 12 below.

### Supplied measurements

These runs used the same load and a 2 GiB limit on 2026-11-16.

| Reader with Python selection | Cold | Warm | Result digest prefix |
| --- | ---: | ---: | --- |
| Python | 704 s | 165 s | `2de7c1bdf58dba21` |
| Rust | 494 s | 132 s | `2de7c1bdf58dba21` |

`scripts/style/refill_measure.py` backs up the ledger through a read-only SQLite connection.
It clears derived tables and starts separate cold and warm processes.
It compares selection, candidates, cells, and declaration proofs.
Its `COUNT` operation does not test ticket publication or the complete `next-phase` command.

“Cold” here means no saved derived results and a new process.
It does not mean an empty operating-system page cache.
Measure the first run after a host restart separately.
Do not clear shared host caches for that measurement.

An earlier Rust probe parsed 25 MB with `syn` in 0.22 seconds using eight threads.
That result excludes selection, Git history, SQLite, and Python communication.
It supports a small parsing budget, not a refill-time prediction.

The supplied 529-second profile sampled stacks every 10 milliseconds.
Its percentages are inclusive and overlap.
Do not add them to predict a speed increase.

| Work | Inclusive share | Design response |
| --- | ---: | --- |
| Coverage and candidate sites | About 35% | Bulk judgment indexes and native candidate enumeration |
| Declaration groups and follow checks | About 34% | Shared declaration, name, and rename indexes |
| Finding footprints | About 12% | Calculate from shared source indexes |
| Candidate tickets | About 11% | One coverage result and ranking pass for all requested families |
| Preparing Rust blob facts | About 9% | Keep facts in Rust; remove Python decoding and compression |
| Git subprocesses | About 12% | In-process Git reads and shared revision comparisons |

Saved-result publication also appears inside these paths.
Changing the store alone cannot remove these calculations or their repeated traversal.

### Measurements from this design round

Two read-only probes ran on natedev under the required 2 GiB scope.
They opened the live ledger with Python `sqlite3` URI `mode=ro` and `query_only=ON`.
Each probe used one read transaction and a 32 MiB SQLite cache.
Neither probe wrote the ledger or ran a refill.

The design checkout is `6270817606ea0546417113e7e4450c4cfa0e3772`.
The observed ledger schema is 46.
Its synced commit is `5142c3ca45e58d90aa2ab65aee58e24e052efcbb`.
The Git probe observed cargo-liner-style HEAD `6f2f75038ef5e6ce26ce75b23a6571113de31362`.

| Observation | Result |
| --- | ---: |
| Ledger file size | 4,417,458,176 bytes |
| Rows outside `refill_*`, `listed_rules`, and `listed_sites` | 142,497 |
| Value bytes in those rows | 66,927,233 bytes |
| Python scan and value-byte accounting for those rows | 0.565 s |
| Judgment rows | 54,320 |
| Held finding rows | 47,579 |
| Rust paths at observed HEAD | 1,306 |
| Rust source bytes at observed HEAD | 25,000,962 bytes |
| First probe, including derived-kind counts and Git inventory | 2.99 s |
| Second probe, including derived payload accounting | 5.90 s |
| Maximum reported Python RSS in either probe | 306,620 KiB |

The value-byte count measures UTF-8 strings, byte arrays, and eight bytes per number.
The derived payload counts below use SQLite byte lengths after conversion to BLOB.
Neither count includes indexes, page slack, or Rust allocation overhead.
The first scan includes small control tables as well as selection inputs.
These probes observed a later live ledger than the supplied refill benchmark.
Host page-cache state was uncontrolled.

| Existing derived data | Rows | Logical payload bytes |
| --- | ---: | ---: |
| `refill_cells` | 74,300 | 97,154,072 |
| `refill_findings` | 21,826 | 362,808,884 |
| `refill_inputs` | 34,913 | 199,467,431 |
| `refill_dependencies` | 168,265 | 29,206,837 |
| `listed_sites` | 462,180 | 108,789,668 |
| `refill_candidates` | 0 | 0 |

`refill_objects` also contains 1,179,579 `finding_word` rows and 1,262 `line_count` rows.
The source-row scan makes a bulk read plausible within one second on this host.
It does not prove the cost of Rust indexes, selection, or durable cache publication.

## Store choice

Keep SQLite as the authoritative ledger.
The store choice is settled for this work.
Do not introduce SpacetimeDB, another database engine, or a service process.

Use `selection.sqlite` beside `ledger.sqlite` for disposable Rust results.
This separates new cache writes from current ledger users.
It permits cache deletion without removing judgments or findings.
The Rust child opens the authoritative ledger read-only.
Python retains all authoritative migrations and writes.

Retain the current ledger journal mode.
Do not include `VACUUM` or file shrinkage in a refill.
The new selector can avoid old derived pages without shrinking the ledger.

## Architecture

### Calculation ownership

Extend `rust/style_reader` with selection modules and a versioned selection request.
Keep the current fact request compatible during overlap.
Reuse its token semantics, Unicode tables, Git reader, and error conventions.
Do not replace the token reader with `syn` during this port.

| Rust stage | Existing Python behavior | Outputs |
| --- | --- | --- |
| Source observation | `refill_inputs`, `RepositoryTip`, rule and revision identities | Source views and source-row indexes |
| Findings | `declarations`, `finding_sites`, declaration methods in `style_ledger` | Declaration outcomes, site evidence, built rename evidence |
| Footprints | `style_footprint`, `approved_renames`, footprint methods in `style_plan` | Source paths, rename permission results, refusal reasons |
| Candidates and coverage | `candidate_generators`, `refill_facts`, `refill_cells`, coverage methods | Listed sites, cells, verdicts, scope comparisons |
| Selection | `ranked_work`, `candidate_tickets`, `_select_observed_phase`, batching methods | Ordered candidates, counts, waits, and proposed actions |

Python keeps the command line, staff lookup, and existing document parsers.
It supplies compact rule definitions and external document observations once per request.
Rust determines eligibility and ranking from those values.
Python keeps ticket allocation, assignment, decision-file writes, plan writes, and worker dispatch.
It also keeps existing mutation locks and recovery behavior.

The port includes selection calculations invoked by reconciliation and publication guards.
Leaving those calculations in Python would move the delay outside the benchmark.
Python writers may request Rust validation through the same child.
They must not repeat source scans or selection in Python.

### Reads and indexes

Read ledger source projections in one short SQLite read transaction.
Read the source-version vector inside that transaction.
Close the transaction before Git analysis and selection.
Do not hold a reader lock during the complete calculation.
For a changed partition, bulk-read its supporting source rows, including unchanged rows needed by its proof.
Do not reconstruct a complete source-row mirror in the cache.

Build these indexes once for the request:

1. Index judgments by rule, path, generation, and judgment order.
2. Index findings by identity, rule, path, proposal, ticket, and declared name.
3. Index ticket relations and current states.
4. Index declarations and names by blob, crate, and revision view.
5. Index revision ancestry, removals, and changed paths.

Preserve Python ordering at each output boundary.
Share the cell population across families and candidate queries.
Share declaration outcomes across selection and the follow step.
Use indexes to select possible declaration matches before applying existing proof rules.
Do not compare every finding with every declaration.

Use `gix` for trees, blobs, revision resolution, ancestry, and comparisons where parity tests establish equivalence.
Memoize distinct revision pairs for the request.
Preserve merge-base, rename, pathspec, and missing-object behavior explicitly.
Matching trees do not imply matching ancestry or removal history.

A missing native Git operation may use a batched Git adapter during construction.
Record its invocation count and elapsed time.
Phase 11 must remove repeated per-cell and per-finding Git processes.
A remaining bounded adapter must pass timing and differential tests.

### Candidate semantics

Port every generator reachable from the rule catalog, including manifest and workspace generators.
Preserve `#uncovered` coverage, candidate text, line numbers, ordering, and ignore behavior.
The source set includes manifests and other selected files, not only Rust blobs.

Regex candidates currently use ripgrep plus Python exclusion patterns.
Pre-filters use Python regular expressions.
These are separate semantics and require separate differential fixtures.

Phase 8 first retains ripgrep behavior in a Rust-owned batch adapter.
Share one temporary source export per distinct selection domain within a refill.
Reuse its path topology and selected contents across compatible rules.
Do not create an export once per rule and file.
The timing gate includes exports and subprocesses.

Port compatible regex operations to native libraries only after proving their behavior.
A bounded compatibility adapter may handle unsupported Python regex constructs during construction.
Expose adapter use in diagnostics and timing results.
Phase 11 requires native support for the pinned rule catalog or a measured adapter within budget.
An unsupported rule must produce a prerequisite error or an explicitly selected Python run.
It must never produce an empty candidate list as a substitute.

### Process boundary and publication

Start one Rust child for one refill command.
Keep that child through mutation acknowledgements and multiple ticket selections within the command.
Use bounded JSON messages for control values and selected outputs.
Do not transfer tokens, complete source facts, or the full cell population in production requests.
Use separate streamed diagnostic output for differential comparisons.

The initial request identifies the repository, ledger, cache, implementation, unit, revisions, selection mode, and external inputs.
The reply contains its observation identity and one selection result or publication step.
No Rust reply directly changes authoritative state.

`COUNT` and assignment differ today.
`COUNT` projects followed findings without writing them.
Assignment can drop a queued ticket, release its findings, and then select those findings.
The protocol must preserve that sequence.

Use this publication sequence:

1. Acquire the existing refill lock in Python.
2. Capture the board, external inputs, and resolved revision identities.
3. Request selection from Rust.
4. Validate the returned source versions under the Python writer transaction.
5. Validate relevant external identities and existing ownership guards.
6. Apply the next existing mutation group through Python writers.
7. Send committed changes and refreshed observations to Rust.
8. Continue selection from the resulting state.

The writer rejects stale results before applying a mutation group.
An external change requires a new observation before further actions.
Permit at most two automatic retries, then return the existing waiting or prerequisite-failure result.
Include retry time in the command timing.

The refill lock does not exclude every judgment writer or external file editor.
The source-version check and final approval check remain necessary.
Retain current transaction boundaries and decision-file recovery order.
Do not hold a SQLite write transaction across a decision command or Git operation.
Request any additional Rust proof before acquiring the writer transaction.
Inside that transaction, compare the proof's versions and apply the existing bounded writer checks.
Do not ask the child to observe uncommitted Python writes through another connection.

The follow step must still skip decisions with missing generated content or a missing Parent line.
An approval change before publication must still prevent a rename from becoming built.
Partial publication must resume through the existing recovery path.
Never save an action sequence as permission to replay writes on a later refill.

### Switch and binary identity

Add `selection=python|rust` beside the existing `reader` setting.
Its default remains `python` until the user authorizes the change.
Rust selection owns its source reader internally.
Python selection continues to honor `reader=python|rust`.

Shadow comparison uses a separate command and private copies.
It does not run the slow selector on every production refill.
It does not write live tickets, plans, or proposals.

Identify a build with its Rust sources, dependency lockfile, protocol version, and ported Python contract digest.
Include relevant workspace settings in that identity.
The current stamp covers only `HEAD:rust/style_reader`; that is insufficient for the expanded selector.
Reject incompatible binaries before selection.

Build and install the binary before enabling the selector.
Do not compile synchronously inside a timed refill.
The explicitly selected Python path remains usable without the new binary.
A requested Rust run with no matching binary returns a prerequisite failure.
Do not hide a slow Python fallback in a successful Rust timing result.

## Warm reuse and change keys

A warm refill compares a small manifest before loading source rows or scanning blobs.
It reuses valid partitions and recalculates affected partitions.
The final busy-file filter and publication checks always use current inputs.

### Durable ledger changes

Reuse `refill_tables` and `refill_rows` for source versions and changed-row keys.
The existing `refill_changed_rows(source_table,version)` index supports incremental reads.
Do not use `PRAGMA data_version` as a cursor across processes.
The existing `selection_version()` value serves one connection, not a persistent cache.

Audit every authoritative table read by selection and publication validation.
Current version triggers cover 18 tables.
They omit tables such as `handoffs`, `bulk_refusals`, and `sync_waits`.
Add source-version triggers for every missing source before enabling warm reuse.
Do not treat the current trigger list as a complete dependency list.

Each insert, update, and deletion advances its table version in the same transaction.
Keep old and new primary keys for key-changing updates.
Keep deletion markers while retained cache cursors require them.
Store a ledger revision epoch to distinguish resets, migrations, and restored state.
An epoch change, version regression, or unavailable change history requires a full calculation.

Change the benchmark's blanket `refill_*` deletion to an explicit derived-table list.
Preserve source versions, deletion markers, and report rows such as `refill_results`.
A reset of source tracking must advance the epoch.
Test resets must follow the same rule.

Current `Ledger` admission rejects a newer schema, including read-only admission.
An additive migration alone therefore does not preserve compatibility with old executables.
Deploy Python admission support for both schemas before migrating the shared ledger.
That compatibility release must keep Python selection and must not apply the new migration during ordinary ledger opening.
Apply the explicit migration only after all active callers use that release.
Measure trigger overhead on current judgment and ticket writes.
Prove Python behavior before and after migration on a private backup.
Rollback selects Python within the compatible release; it does not restore a schema-46 executable.

### Input identities

| Change key | Contents | Affected calculation |
| --- | --- | --- |
| Implementation | Protocol, selector semantics, reader semantics, rule interpreter, dependencies | All partitions |
| Ledger identity | Repository identity, database identity, schema, revision epoch | All partitions |
| Ledger cursor | Version per source table; changed keys and deletion markers | Subscribers of old and new row membership |
| Revision roles | Synced commit, unit tip, main tip, pushed tip, checkpoints, branch presence | Ancestry, removals, scope matches, and selection |
| Source membership | Trees, paths, modes, blobs, submodules, manifests | Source scopes and membership queries |
| Rule catalog | Rule identities, judged bodies, generations, reach, tier, family, generators | Rule partitions and ranking |
| Decisions | Contents, presence, approval, status, Parent, generated-body fields | Related findings and decision selection |
| Work assignment | Staff revision, production document, plan, family scopes, first files, ticket limits | Unit eligibility, ranking, batching |
| Availability | Board, ownership, yields, checkpoints, pending sync, worktree observations | Final selection and publication |
| Tool environment | Locale, ripgrep executable/configuration, external ignores, lint configuration | Search, generators, lint coverage |
| Git availability | Required object identities and current availability | Every reused result requiring those objects |

Hash relevant external file contents once per observation.
Include additions, deletions, and directory membership in the manifest.
Do not use only modification time and file size as durable identities.
Two edits can preserve both values.

Resolve mutable Git refs once per observation and recheck relevant refs before publication.
The worktree identity includes every file that current selection reads from the worktree.
Do not replace those reads with HEAD contents.

### Recalculation boundaries

Keep coverage partitions by rule and crate.
Keep finding partitions by source view and search domain.
Keep generator partitions by specification and its file, crate, or workspace domain.
Retain canonical ordered rows inside each partition.

Store subscriptions to semantic domains, including queries that found nothing.
Examples include a rule/path judgment group, a proposal, a declaration name, and manifest membership.
Do not store one dependency row per token, candidate site, or Git object.

A changed row maps through both its previous cached membership and its current membership.
This invalidates the old path when a finding moves.
A deletion uses cached membership and its deletion marker.
A missing membership map invalidates the complete affected stage.

A changed tree produces added, removed, mode-changed, and content-changed paths.
Use old and new declaration names to invalidate name-search domains.
An added declaration must invalidate a previous “not found” result.
An outside-rename search can cross crate boundaries.
Its subscription must cover that complete search domain.

Built-rename membership, rule generations, removal barriers, and ancestry can affect many partitions.
Recalculate the complete stage when a smaller affected set cannot be proved.
Correctness takes priority over a small-change timing target.

An unchanged warm refill reads its manifest and only compact outputs needed for the unit.
It performs no tokenization, candidate enumeration, declaration proof, or coverage calculation.
It still checks required Git objects in one deduplicated batch.
A cache hit does not prove that an old object still exists.
Unavailable objects invalidate dependent results and invoke the existing missing-object behavior.
Preserve Python cases that skip unavailable judgment history instead of refusing the complete refill.
Do not save missing-object failures as completed results.
Object restoration must permit a subsequent successful calculation.

An ordinary changed warm case changes at most ten files, one decision, or one hundred ledger rows.
The below-three-second target applies when those changes affect a bounded set of partitions.
A catalog replacement, history rewrite, or broad rename dependency can require a cold-sized calculation.
Measure those cases separately and apply the cold target to them.

## Saved results

The cache stores completed selection partitions, not another copy of source history.
Source rows remain authoritative only in `ledger.sqlite`.

| Existing saved kind | Rust disposition | Reason |
| --- | --- | --- |
| `tree`, `tree_entry`, `source_text`, `masked_source`, tokens, line counts | Request memory only | Read each required blob once; avoid source duplication |
| `finding_word`, `finding_words` | Request search indexes only | More than one million rows represent a small source corpus |
| `finding_declarations`, `finding_module_links`, `footprint_modules`, `rust_code` | Request facts only | Reuse the native reader and remove Python encoding |
| `ancestor`, `scope_comparison` | Request maps only | Batch revision work; retain resulting coverage partitions |
| Per-file `generator`, trait declarations, trait implementation counts | Request facts and indexes | Share scans and cross-file counts |
| `refill_candidates`, `listed_rules`, `listed_sites` | Compact generator partitions | Preserve completed membership and candidate results |
| `refill_cells` | Compact coverage partitions | Preserve judgment results without repeated identities and object lists |
| `refill_findings` views `synced` and `built` | Compact finding partitions | Preserve follow proofs and built-rename results by domain |
| `refill_findings` views `footprint:*` | No separate saved layer | Derive paths from shared indexes; include required paths in finding partitions |
| `object_group` and repeated object lists | One required-object manifest per observation | Preserve availability checks without repeated arrays |
| `refill_dependencies` | Coarse partition subscriptions | Preserve mutable and negative-query dependencies without per-result expansion |
| `refill_tables`, `refill_rows` | Retain and complete source coverage | Detect authoritative changes across processes |
| `refill_results` | Retain as report state | It records user-visible operation results |

“Request only” is a design choice to test, not a measured native recomputation cost.
Phase 6 measures saved bytes and repeated work by kind.
Phases 7–10 compare native recomputation with compact cache reads.
If recomputation exceeds its budget, retain the smallest shared immutable index with a measured benefit.
Do not restore the old graph of individual saved facts by default.

Use these cache tables:

| Table | Purpose |
| --- | --- |
| `selection_observations` | Completed manifest, source cursors, implementation identity, partition references |
| `selection_partitions` | Versioned payloads for coverage, generators, and finding outcomes |
| `selection_subscriptions` | Domains that invalidate partitions, including empty query results |
| `selection_source_routes` | Previous row membership needed for changes and deletions |
| `selection_required_objects` | Deduplicated requirements shared by completed observations |

Use compact typed payloads with explicit format versions.
Intern repeated paths, rules, names, and candidate text within each payload.
Represent source-backed text by blob and source position where exact reconstruction is possible.
Retain literal candidate text when the generator transforms it.
Do not serialize complete finding records inside every proof.

Publish changed partitions and their completed observation in one cache transaction.
Do not mark a partial observation complete.
After a crash, reuse the previous complete observation or calculate again.
A damaged cache permits reconstruction; a damaged source ledger returns an error.
Cache failure must not silently change an authoritative result.

Retain current observations for configured units and one previous observation for recovery.
Prune unreferenced partitions after publication with a bounded deletion budget.
Measure logical cache size and bytes written separately.
The initial cold cache budget is 64 MiB.
The ordinary changed-refill budget is 4 MiB written.
These are engineering budgets, not measurements.

During overlap, leave Python derived tables and consumers intact.
The Rust path neither populates nor depends on them.
At phase 12, audit consumers before deleting any table or reader.
Reports and remaining ledger methods must receive equivalent projections first.

## Types and resource bounds

Apply `~/.claude/docs/type_design.md` to every new type and protocol value.
Names must describe a role, state, or guarantee.
Preserve existing domain distinctions at the wire boundary.

| Type | Meaning |
| --- | --- |
| `ObservedSelectionInputs` | Inputs for one attempt; publication has not validated them |
| `SelectionObservationId` | Identity of source versions, external inputs, and implementation |
| `SelectionMode::Count` / `SelectionMode::Assign` | Project changes or request authoritative publication |
| `UnitBranchState::Present` / `UnitBranchState::NotCreated` | Explicit branch availability |
| `CacheValidity` | `Current`, `Absent`, `Invalidated`, `UnsupportedVersion`, or `Damaged` |
| `SourceRowChange` | `Present` with a row identity, or `Deleted` with its previous identity |
| `DeclarationOutcome` | Existing moved, unchanged, removed, uncertain, and approval-dependent outcomes |
| `SelectionProgress` | `NeedsPublication`, `Complete`, `Waiting`, or `InputsChanged` |
| `ValidatedPublication` | Python-side value valid only inside the writer's current validation scope |
| `SelectionError` | Named `thiserror` variants for source, protocol, prerequisite, and resource failures |

Convert library `Option` values and boundary booleans into explicit domain states.
Do not use a missing value for both “not read” and “proved absent.”
Keep limits and version constants in `constants.rs`.
Use spelled-out names and no `#[allow]` attributes.
Pin new workspace dependencies with `=`.
Keep Rust unit tests in the file under test.
Use tempdir repositories for integration and differential fixtures.

Start with eight parsing workers and a bounded queue.
Share compact facts and release token buffers after their consumers finish.
Do not retain source ASTs or all token vectors for every historical blob.
Include indexes and pending serialization in the resource budget.

| Resource | Initial budget |
| --- | ---: |
| Authoritative rows and selection indexes | 384 MiB |
| Git objects, trees, compact facts, parsing workers | 512 MiB |
| Partition payloads and output batches | 256 MiB |
| Python, SQLite caches, protocol buffers | 256 MiB |
| Remaining cgroup margin, including file-cache charges | 640 MiB |

These budgets total 2 GiB.
They are allocation limits to verify, not observed Rust memory.
Reduce workers and release request caches before exceeding the limits.
Return a named resource error if the input still cannot fit.
Never truncate findings or candidates to fit memory.
Record the cgroup peak as well as simultaneous process RSS.

## Differential proof and overlap

Extend the differential pattern from phases 1–4 to each selection stage.
Run both implementations against identical immutable input captures.
Sequential reads of a changing live ledger do not prove equality.

Freeze these inputs in the parity harness:

1. Capture a private SQLite backup and its source versions.
2. Pin exact Git commits and required objects.
3. Capture rule bytes and external configuration.
4. Capture proposal files, plans, staff values, and board values.
5. Record locale, tool identities, and selection mode.

The fixture harness creates temporary repositories and separate copies of mutable outputs.
It runs current Python as the reference.
It compares typed intermediate values before comparing the final digest.
Canonicalize only explicitly unordered sets and nondeterministic test identifiers.
Preserve candidate order, tie breaks, reasons, paths, lines, and refusal classes.

The current digest is necessary but insufficient for assignment.
Test `COUNT`, assignment, queued-ticket reuse, and multiple tickets.
Compare normalized action traces, authoritative rows, and document bytes.
Inject faults between mutation groups.
Verify that retries neither duplicate tickets nor omit decision recovery.

Every ported module belongs to a dependency manifest used by the parity gate.
A Python change in that manifest requires stage parity on the same candidate revision.
A behavior change requires its Rust change in the same integration change.
Unrelated Python changes do not require a selector rebuild.
An intentional behavior change first receives its own approved specification and fixture.
Do not resolve a differential failure by weakening the comparison.

Current units may continue changing Python during construction.
Integrate their changes at least once per implementation day.
The integration owner handles parity failures and contract-digest changes.
Do not leave that responsibility with an unowned future synchronization task.

Compare warm reuse with full reconstruction after each change sequence:

1. Insert, update, and delete each source-row kind.
2. Move a finding between paths, crates, proposals, and tickets.
3. Add a declaration that changes an empty search result.
4. Change a built rename or rule generation.
5. Delete and restore a file or required Git object.
6. Move a ref without changing its tree, or rewrite its ancestry.
7. Change approval during selection or publication.
8. Change a rule, ignore file, locale, lint configuration, or regex configuration.
9. Change busy files, ticket capacity, family scope, or a checkpoint.
10. Interrupt cache or authoritative publication between committed steps.

Keep concurrency and stale-observation tests in the normal targeted suite.
Use captured cargo-liner inputs for acceptance measurements, not as a live test dependency.

The planned overlap ends with phase 12.
The estimates imply 66–110 active hours on the critical dependency path.
They assume phases 7 and 8 run concurrently after phase 6.
Build waiting, user decisions, and observation time are additional.
Re-estimate after phase 6 and the first complete differential run.

## Expected times

Every row after phase 5 is an estimate.
The profile gives priorities, not independent subtractable time components.
Intermediate ranges include temporary adapters and Python serialization.
They do not authorize an intermediate default switch.

| Completed point | Cold | Warm | Basis |
| --- | ---: | ---: | --- |
| 5: current Rust reader | **494 s measured** | **132 s measured** | Supplied equal-load benchmark |
| 6: contracts and measurement | 490–540 s | 130–145 s | No selection removal; instrumentation overhead |
| 7 alone: findings and footprints | 300–430 s | 85–135 s | Removes much of two overlapping finding paths; Python coverage remains |
| 8 alone: candidates and coverage | 320–450 s | 85–135 s | Removes much of coverage and enumeration; Python findings remain |
| 7 and 8 integrated | 160–300 s | 45–100 s | Both large stages move; orchestration and transfer remain |
| 9: complete native selection | 12–30 s | 6–15 s | Bulk reads and shared indexes replace repeated Python traversal |
| 10: compact partitions and changes | 8–18 s | 1–4 s unchanged; 2–6 s ordinary changes | Warm runs skip proofs; cold still needs measured tuning |
| 11: acceptance | 4–9 s | 0.6–2 s unchanged; 1–2.8 s ordinary changes | Engineering budgets below; actual gate results required |
| 12: deployment | Same as phase 11 | Same as phase 11 | Deployment adds no selection work |

The phase 9 improvement requires removal of repeated work, not just translation of loops.
The phase 11 ranges have low confidence until a complete native selector exists.
If a range fails, retain the old default and revise the affected phase.
Do not label a result in minutes as completion of the seconds target.

| End-state stage | Cold budget | Ordinary warm budget | Evidence or assumption |
| --- | ---: | ---: | --- |
| Python entry and external capture | 0.5 s | 0.5 s | Unmeasured; phase 6 isolates it |
| Ledger observation and cache reads | 1.0 s | 0.25 s | The source scan took 0.565 s; native reads remain unmeasured |
| Git availability, trees, facts | 2.0 s | 0.35 s | Small corpus and prior parse probe; history checks need measurement |
| Candidates, coverage, finding proofs | 3.0 s | 0.6 s | Unmeasured native indexes; no multiplicative scans |
| Ranking and result encoding | 0.5 s | 0.2 s | Compact outputs and shared cells |
| Cache publication and local writers | 1.0 s | 0.4 s | One cache transaction and bounded changes; unmeasured |
| Remaining time margin | 1.0 s | 0.5 s | Allows variation within the targets |
| Total | 9.0 s | 2.8 s | Budget, not prediction evidence |

Measure both the selection benchmark and the complete command through durable local publication.
Report build waiting and worker dispatch separately from refill work.
Do not exclude proposal commands, reconciliation, cache writes, or retries from command timing.
A command with many authoritative edits must report those edits and their cost separately.
Acceptance fixtures include ordinary ticket assignment and declaration-follow publication.

## Selection contract

Phase 6 measures the existing Python `COUNT` calculation.
It adds no production selector or publication path.
The native request below defines the boundary for phases 7–12.
Its schema admission and source triggers remain phase 10 work.

### Request and reply

The capture CLI uses `capture --repo --ledger --unit --folder`.
The replay CLI uses `replay --folder --workspace --reader python|rust`.
The measurement CLI uses `measure --folder --workspace --reader rust --stages`.
A replay creates a new ledger copy and clears only derived tables.
A measurement creates separate cold and warm worker processes.

| JSON value | Required fields and meaning |
| --- | --- |
| Capture manifest | `format: 1`, `repo`, `unit`, `schema`, `schema_version`, `versions`, `revisions`, `references`, `objects`, `files`, `absent_inputs`, `directories`, `worktree_status`, `environment`, `tools`, `board` |
| Copied file | `source`, relative `copy`, `sha256`, `kind`, `replay`; every copied input has a hash |
| Source projection | `table`, ordered `columns`, `version_triggers`; an empty trigger list means no direct source-version trigger |
| Revision observation | Synced commit, unit/main/pushed tips, checkpoint pairs, and explicit `absent` references |
| Object observation | Object ID to Git type or `missing`; objects remain in the source repository |
| Replay report | `result_digest`, `external_changes`, `cleared_tables`, `stored`, `stages`, `run` |
| External change | `source`, `expected`, `actual`; values are SHA-256 hashes, revision IDs, or `missing` |
| Stage report | `seconds`, `calls`, `counters`, `git_commands`, `repeated_work` |
| Proposed native request | `protocol`, `mode`, `repository`, `observation`, `build`, `unit`, `families`, `policy`, `external_inputs` |
| Proposed observation | Ledger identity, schema, version vector, source projections, resolved revisions, and required object availability |
| Proposed native reply | Observation identity, ordered stage values, counts, ordered candidate tickets, ordered reasons, object requirements, and proposed actions |

The native schema definitions below describe the proposed boundary.
They do not add validation to production in phase 6.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$defs": {
    "request": {
      "type": "object",
      "required": ["protocol", "mode", "repository", "observation", "build", "unit", "families", "policy", "external_inputs"],
      "properties": {
        "protocol": {"const": 1},
        "mode": {"enum": ["count", "assign"]},
        "repository": {"type": "string"},
        "observation": {"$ref": "#/$defs/observation"},
        "build": {"type": "object", "additionalProperties": {"type": "string"}},
        "unit": {"type": "string"},
        "families": {"type": "array", "items": {"enum": ["names", "types", "values", "modules"]}},
        "policy": {"type": "object"},
        "external_inputs": {"type": "array", "items": {"type": "object", "required": ["source", "sha256", "kind"]}}
      }
    },
    "observation": {
      "type": "object",
      "required": ["database", "schema", "source_versions", "revisions", "source_projection", "object_requirements"],
      "properties": {
        "database": {"type": "string"},
        "schema": {"type": "integer"},
        "source_versions": {"type": "object", "additionalProperties": {"type": "integer"}},
        "revisions": {"type": "object", "additionalProperties": {"type": "string"}},
        "source_projection": {"type": "array", "items": {"type": "object", "required": ["table", "columns"]}},
        "object_requirements": {"type": "array", "items": {"type": "string"}}
      }
    },
    "reply": {
      "type": "object",
      "required": ["observation_identity", "stages", "counts", "candidates", "reasons", "objects", "actions"],
      "properties": {
        "observation_identity": {"type": "string"},
        "stages": {"type": "array", "items": {"type": "object"}},
        "counts": {"type": "object", "additionalProperties": {"type": "integer"}},
        "candidates": {"type": "array", "items": {"type": "object"}},
        "reasons": {"type": "array", "items": {"type": "object"}},
        "objects": {"type": "array", "items": {"type": "string"}},
        "actions": {"type": "array", "items": {"type": "object"}}
      }
    }
  }
}
```

The current digest covers selection, candidate tickets, cells, and declaration proofs.
It preserves order, paths, lines, reasons, and refusal types.
It canonicalizes dictionary keys and explicitly unordered sets.
It excludes measurements and process identities.
`COUNT` does not prove assignment, recovery, dispatch, or concurrent publication.
Phase 9 must compare those behaviors before the native selector replaces them.

### Build identity and external reads

| Identity | Captured fields |
| --- | --- |
| Implementation | Git commit, tracked selection diff hash, and hashes of all Python selection files, including new files |
| Reader | Committed reader source tree ID and installed binary stamp bytes |
| Tools | Python version, Git version, and complete ripgrep version output |
| Locale | `LC_ALL`, `LC_CTYPE`, `LANG`; unset values remain unset |
| Rule interpreter | Captured rule bytes, rule blob IDs in the copied source rows, and implementation identity |

| External input | Capture | Replay |
| --- | --- | --- |
| Style configuration | Effective repository and local `*.conf` files | Private `CONF_DIR`; only its reader setting changes |
| Global rules | Every `rust/*.md` file | `STYLE_HISTORY_NATE_STYLE_DIR` selects captured files |
| Staff rows | The staff JSON file | `STAFF_FILE` selects the captured file |
| Staff configuration | Explicit staff configuration, when set | Existing `STAFF_CONFIG` selects the captured file |
| Lint configuration | Global cargo-mend configuration | Existing `XDG_CONFIG_HOME` selects the captured file |
| Git XDG configuration | The Git configuration under the original XDG root | Existing `XDG_CONFIG_HOME` selects the captured file |
| Ripgrep configuration | Configured file and referenced ignore files | Existing `RIPGREP_CONFIG_PATH` selects captured configuration; referenced ignore paths retain hash comparison |
| Repository rules and manifests | Files from the repository | Original paths; hash comparison |
| Ignore and attribute files | `.gitignore`, `.ignore`, `.rgignore`, Git excludes and attributes | Original paths; hash comparison |
| Other Git configuration | Local Git configuration and `~/.gitconfig` | Original paths; hash comparison |
| Implementation policies | TOML files under `config`, `scripts/style`, and `scripts/production` | Original paths; hash comparison |
| Ripgrep executable | Executable bytes and complete version output | Original executable; hash comparison |
| Production document and plans | Production document, unit `.unit.toml` files, and resolved unit plans | Original paths; hash comparison |
| Proposals | Proposal Markdown files and directory membership | Original paths; file and membership hash comparison |
| Working files | Tracked and untracked files plus Rust and ignore files from participating worktrees | Original paths; file and Git status comparison |
| Board | JSON from `cargo-berth board --json`, including a nonzero-status envelope | Read the board again and compare the JSON hash |
| Revisions | Resolved named references and presence | Resolve again and report changed IDs or presence |
| Git objects | IDs and availability | Keep the source object store; the existing reader handles missing objects |

Replay compares unconfigured inputs before selection.
It reports differences and adds no refusal.
It does not freeze unconfigured inputs during selection.
The production proof must report those differences beside its digest.
Phase 9 owns concurrent-change fixtures and stale-publication rejection.

### Mutation boundaries

The following order describes the existing path and the required native publication boundary.
Only derived cache publication occurs in this phase's private `COUNT` runs.

1. Observe source rows, rules, revisions, approvals, and busy files.
2. Calculate declaration follows, coverage, footprints, rank, sync waits, and candidate order.
3. Publish derived facts separately from authoritative ticket changes.
4. Validate the observation before authoritative publication in phase 9.
5. Apply existing finding follows, withdrawals, waivers, and decision closures in their existing order.
6. Allocate or reuse the ticket and record its approved findings and pinned rules.
7. Assign the ticket and start its proposal documents.
8. Write the phase document and preserve existing recovery records.
9. Dispatch the worker through the existing Python adapter.

The native port must preserve each existing transaction and recovery boundary.
It must not combine document changes with an assumed SQLite transaction.

### Source projection audit

The manifest records every column and trigger name from the admitted schema.
The table below records schema 47 from the production backup.
It includes a conservative source superset for selection and later publication checks.
No missing trigger becomes an implicit warm-reuse guarantee.

| Table | Columns | Direct version trigger |
| --- | --- | --- |
| `bulk_refusals` | `finding_id, ticket, reason, at` | missing |
| `bulk_workspace_tickets` | `ticket, batch` | missing |
| `finding_needs` | `finding_id, path, recorded_at` | present |
| `fold_withdrawals` | `finding_id, unfolded_at` | missing |
| `handoffs` | `checkpoint, state, merge_commit, sent_to, created_at, updated_at` | missing |
| `held_cell_corrections` | `judgment_id, ticket, rule, path, old_outcome, new_outcome, finding_ids, corrected_at` | present |
| `held_findings` | `finding_id, rule, path, line, text, kind, before, after, rule_blob, ticket, state, proposal, created_at, updated_at, build_ticket, moved_from, withdrawn_by, person_read_by, duplicate_of, unwaived_by, followed_from, followed_from_line, followed_at, declaration_gone_at, waive_provenance, waived_by, built_at, anchor_commit, followed_from_commit` | present |
| `invalidated_held_judgments` | `judgment_id, recorded_at` | present |
| `judgments` | `id, rule, rule_blob, input_scope, input_fingerprint, path, commit, verdict, verdict_detail, ticket, worker, recorded_at, declared_scope, read_files, generation` | present |
| `lint_runs` | `id, tip, tree, tool, scope, config_hash, passed, diagnostics_json, rule_blobs_json` | present |
| `processed_tips` | `tip, processed_at, reason` | missing |
| `reconciliation` | `id, branch_point` | missing |
| `refill_results` | `unit, at, outcome, split_at, ready, changed, structure, other` | missing |
| `refill_rows` | `source_table, source_key, version, present` | missing |
| `refill_tables` | `source_table, version, applied_version` | missing |
| `rename_dispositions` | `finding_id, name, line, text, kind, reason` | present |
| `rename_outer_binding_blocks` | `finding_id, name, line, text` | present |
| `rule_body_withdrawals` | `finding_id, rule_blob` | present |
| `rule_candidates` | `ticket, unit, path, line, idea, created_at` | missing |
| `rule_generations` | `rule, rule_blob, generation` | present |
| `rule_texts` | `blob, text` | present |
| `schema_version` | `version` | missing |
| `sync` | `id, tip, style_branch, synced_at` | present |
| `sync_waits` | `unit, since, token` | missing |
| `ticket_batch_owners` | `ticket, batch` | present |
| `ticket_counter` | `id, value` | missing |
| `ticket_predecessors` | `ticket, predecessor` | present |
| `ticket_proposals` | `ticket, proposal` | present |
| `ticket_widened_paths` | `ticket, path, reason, widened_by, widened_at` | present |
| `ticket_yields` | `ticket, path, recorded_at` | present |
| `tickets` | `id, unit, kind, family, rule, crate, files, sites, rules, rule_blobs, state, branch, checkpoint, created_at, updated_at, base, proposal, cells, finding_ids, reservation_session_dir, reservation_phase` | present |

`refill_tables`, `refill_rows`, and `refill_results` retain source tracking and report state during benchmark resets.
The reset clears `refill_dependencies`, `refill_candidates`, `refill_cells`, `refill_findings`, `refill_inputs`, `refill_objects`, `listed_sites`, and `listed_rules`.
The audit does not add triggers or a schema-admission migration.
Phase 10 must establish both before native warm reuse.

### Stage proof

The wrappers run only inside the measurement worker.
The wrappers measure exclusive time across instrumented boundaries.
The reader time includes request lifetime and Python JSON consumption.
The external time includes rule parsing.
The encoding time includes derived result encoding and identity encoding.
SQLite read time includes source reads and derived reads.
Uninstrumented work and wrapper overhead remain in the wall time.
Process-tree RSS can count shared pages more than once.
Each command still runs inside `MemoryMax=2G`, with no swap.

Production capture: `/build/claude/scratch/sdlc-style-impl-7638dd-capture`.
The capture contains 7,892 files and schema 47.
It records 31 source tables, including 18 tables with direct version triggers.
Of 3,257 recorded Git IDs, 3,160 are present and 97 are missing.
The manifest records each missing ID.
The existing readers retain their missing-object behavior.

Every replay and both measured runs produced this digest:

`4bda89230904df370397d43172f948c5973d54618911ba775816558f3c1fe92b`

| Replay | Wall seconds | Process-tree peak RSS bytes |
| --- | ---: | ---: |
| rust-1 | 350.8289655779954 | 2478026752 |
| rust-2 | 288.27590806098306 | 2502213632 |
| python-1 | 346.79425739400904 | 1970212864 |
| python-2 | 439.91174457300804 | 2149498880 |

| Rust stage | Cold seconds | Warm seconds |
| --- | ---: | ---: |
| Python start | 0.9711234039859846 | 0.9197803089919034 |
| SQLite reads | 17.535391288722167 | 15.345986451604404 |
| External reads and rule parsing | 6.808229167800164 | 6.164705216069706 |
| Shared Git boundary | 14.725822237407556 | 3.319966431503417 |
| Reader requests and consumption | 32.964953131595394 | 7.449806957942201 |
| Derived and identity encoding | 11.402108143578516 | 2.5921664996421896 |
| SQLite writes and publication | 8.589815672603436 | 0.025341950939036906 |
| Instrumented declaration comparisons | 32.43234390389989 | 0.0 |
| Other work and instrumentation | 147.20881067839218 | 30.24807609830168 |
| Complete worker wall time | 272.6385976279853 | 66.06582991499454 |

The Git time covers `run_git` calls.
The subprocess counter also includes Git starts outside that boundary.
The revision-pair count uses distinct argument pairs for `diff`, `merge-base`, and `rev-list`.
It does not add resolution calls.

| Counter | Rust cold | Rust warm |
| --- | ---: | ---: |
| Filed findings | 14726 | 14726 |
| Candidate tickets | 256 | 256 |
| Git subprocess starts | 11052 | 2671 |
| Reader requests | 689 | 15 |
| Distinct revision argument pairs | 1188 | 23 |
| Declaration searches | 21397 | 0 |
| Declaration comparisons | 1563913 | 0 |
| Rule catalog loads | 93 | 93 |
| Rule file loads | 6603 | 6603 |
| Distinct rule file paths | 82 | 82 |

The cold declaration loop makes 21,397 searches and 1,563,913 item comparisons.
Its inner work grows with findings and candidate declarations.
The rule loop makes 93 catalog loads and 6,603 file loads across 82 physical paths.
Each catalog load scans its selected rule files again.
Warm reuse removes declaration comparisons but retains repeated rule catalog loads.

The measured cold peak is 2500218880 bytes of summed process-tree RSS.
The warm peak is 912678912 bytes.
Shared pages can appear in more than one process's RSS.
The enforced cgroup limit remains 2 GiB.
These sequential runs do not establish a speed improvement over phase 5.

The replay reports record board changes and proposal directory membership changes.
Those unconfigured inputs remain live, as the capture policy requires.
Digest equality does not prove that those inputs stayed frozen during selection.

All temporary replay and measurement workspaces were removed.
The capture remains available for the director.
The full Python suite passed 2,347 tests, with 27 skipped.
The focused pair passed three tests.
The four changed Python files passed types with zero errors, warnings, or notes.


## Phases

Estimates are active elapsed hours for one director and a shared pool of two Codex workers.
The director owns integration and review.
During concurrent phases 7 and 8, each phase receives one worker.
The estimates exclude build waiting and user-response time.
Each phase must finish its proof before dependent integration.

| # | Status | Phase | Dependencies | Proof | Estimate |
| --- | --- | --- | --- | --- | --- |
| 1 | done | Git reader, tokens, lines, names | — | Existing blob differential suite | Complete |
| 2 | done | Native fact ports | — | Existing per-kind differential suite | Complete |
| 3 | done | Python client and reader switch | — | Equal refill results | Complete |
| 4 | done | Remove Rust blob persistence | — | Equal result, row and time measurements | Complete |
| 5 | done | Module-scan declarations and crate-root reuse | — | Stage differential proof | Complete |
| 6 | done | Observation contract, parity capture, source audit, stage measurements | **Depends on:** 5 | Frozen-input repeatability; complete source inventory; concurrent-write fixtures | 6–10 h |
| 7 | todo | Declaration outcomes, site evidence, rename permission, footprints | **Depends on:** 6 | Ordered stage values and failures equal Python; bounded memory | 20–32 h |
| 8 | todo | Candidate generators, judgment coverage, scope comparisons | **Depends on:** 6 | Listed sites, cells, verdicts, and ordering equal Python | 16–24 h |
| 9 | todo | Complete selection and Python publication adapter | **Depends on:** 7, 8 | `COUNT` and assignment traces equal Python; fault and race tests pass | 16–24 h |
| 10 | todo | Compact cache, source changes, partition invalidation | **Depends on:** 9 | Mutation sequences equal reconstruction; crash recovery passes | 12–20 h |
| 11 | todo | Timing, memory, installation, Mac proof | **Depends on:** 10 | Cold/warm targets; complete command proof; platform parity | 8–16 h |
| 12 | todo | Shadow comparison, authorized switch, removal | **Depends on:** 11 | No unresolved differences; rollback proof; consumer audit | 4–8 h work, plus proposed 2 days observation |

### Phase 6 outputs

Define the request schema, mutation boundaries, build identity, and source projection list.
Measure startup, authoritative reads, external files, Git checks, and cache encoding separately.
Count unique revision pairs, declaration searches, rule scans, and cross-process calls.
Record candidate and finding counts beside timing results.
Identify quadratic loops before translating them.

Add capture and replay without changing default selection.
Audit source triggers and benchmark reset behavior.
Define the compatible schema admission release in this document.
Revise estimates and budgets from those results.

Director decisions at phase start (2026-11-16):

1. Read consistency and stale-publication fixtures move to phase 9. The Rust observation read and the publication check do not exist before phase 9.
2. The schema admission release and its migration test move to phase 10. Phase 10 adds the missing triggers, and the migration goes with them.
3. Phase 6 adds no production code path. Counters and timers run only inside the measure worker.

#### Work Order

**Goal:** A frozen capture of the selection inputs, a repeatable replay, a stage measurement on the capture, and the written contract for phases 7–12.

**Depends on:** Phase 5

**Spec:**

1. Capture. A new command makes a capture folder from a repository, a ledger and a unit. It copies the ledger through a `mode=ro` backup. It records the schema, the `refill_tables` version vector and the resolved revision ids (synced, unit tip, main tip, pushed tip, checkpoints, branch presence). It copies each external input that selection reads: rule files, style configuration, ignore files, proposal files, plans, staff values and board values. It records locale, the ripgrep version, the Python version and the reader binary stamp. A `manifest.json` gives a SHA-256 for each copied file.
2. Replay. A replay runs the current Python `COUNT` refill on a fresh copy of the captured ledger, with `--reader python|rust`. It reads the captured external inputs where an existing setting can point there. For each read that has no setting, the replay compares the live file hash with the manifest and reports each difference. It adds no refusal. Two replays of one capture give the same digest.
3. Benchmark reset. `refill_measure.clear_derived` deletes an explicit list of derived tables. It keeps `refill_tables`, `refill_rows` and `refill_results`.
4. Stage measurement. A measure option times these stages separately: Python start, ledger reads, external file reads, Git subprocess calls, reader child calls, derived-result encoding and writes. It counts unique revision pairs, declaration searches, rule scans, Git subprocesses and reader requests. It reports candidate and finding counts beside the times. It wraps functions only inside the measure worker. Default selection does not change.
5. Repeated work. The measurement names each loop whose call count grows with the product of two inputs (for example findings times declarations), with the counts.
6. Contract. Add a `## Selection contract` section to this document. It gives the request and reply JSON schema, the mutation groups and their order, the build identity fields, the source projection list (table, columns, version trigger present or missing) and the external input list from the capture.
7. Run capture, two replays (python and rust) and the stage measurement on cargo-liner-style. Report the stage table in the summary.

**Files:**

- `scripts/style/selection_capture.py` (new) — capture and replay
- `scripts/style/refill_measure.py` — reset list, stage option
- `scripts/style/test_refill_measure.py` — rows for the reset list and the stage option
- `scripts/style/test_selection_capture.py` (new) — capture and replay on a tempdir repository and ledger
- `docs/plans/style-refill-rust-store.md` — `## Selection contract`

**Workers:** 1 writer + 1 tester; the code and the contract do not split.

- `impl`: `selection_capture.py`, `refill_measure.py`, the plan section (hub: none)
- `test`: `test_selection_capture.py` and the rows in `test_refill_measure.py`, from the Spec

**State:** none on the live ledger; captures are private folders.

**Constraints from prior phases:** Phase 5 rust cold 494 s, warm 132 s; python cold 704 s, warm 165 s; digest `2de7c1bdf58dba21` at the then-live ledger. The live ledger opens only with `mode=ro`. `~/rust/cargo-liner-style` is read-only.

**Acceptance gate:**

- Two replays of one capture give one digest, for each reader.
- `basedpyright` reports 0 errors and 0 warnings on each changed Python file.
- `python3 -m unittest scripts.style.test_selection_capture scripts.style.test_refill_measure` passes.
- The plan has the `## Selection contract` section and the stage table.

### Phases 7 and 8 boundaries

Phase 7 owns finding and footprint modules.
Phase 8 owns generator and coverage modules.
Both use the phase 6 source contract.
Changes to shared types require integration review.

Use stage adapters for tests and explicit experimental runs.
Do not insert a Rust request per finding or cell into normal refill.
Retain complete stage results within the request where temporary Python adapters need them.

Phase 7 covers outside renames, ambiguous declarations, moved sites, and approval-dependent results.
Phase 8 covers active generators, partial coverage, lint coverage, removal history, and pending merges.
The corpus must include malformed Rust, Unicode, invalid UTF-8, and path attributes.

### Phase 9 outputs

Move family ranking, queued-ticket handling, batching, sync waits, and checkpoint selection into Rust.
Return actions and compact status values through the publication protocol.
Port expensive preflight and writer-validation calculations reached by the complete command.
Keep existing writer behavior and recovery order.

Compare full refill outputs on captured cargo-liner inputs.
Compare authoritative assignment behavior on tempdir repositories.
Record residual Python time and every subprocess before phase 10.

### Phase 10 outputs

Implement the cache schema and invalidation rules above.
Keep cache writes separate from authoritative publication.
Add trigger coverage for every source in the audit.
Deploy schema compatibility before the explicit tracking migration.
Prove negative-search invalidation, deletion handling, epoch resets, and missing-object recovery.

Verify unchanged warm runs perform zero candidate, coverage, and declaration calculations.
Verify ordinary changes recalculate only affected domains.
Measure broad invalidations separately.

### Phase 11 acceptance

Use release binaries built through the approved build system.
Use the same pinned inputs and comparable host load for both implementations.
Run at least three cold trials and twenty warm trials in fresh processes.
Report every trial, the median, and the warm 95th percentile.
Require every cold trial below 10 seconds and the warm 95th percentile below 3 seconds.
Repeat this check for the complete ordinary refill command.

Include unchanged, source-edit, ledger-edit, and decision-edit warm cases.
Compare outputs with fresh reconstruction.
Report the cgroup peak, bytes written, cache size, process count, and result digest.
Reject results that depend on unbounded memory or hidden Python calculations.

Use this scope for heavy natedev probes:

```sh
systemd-run --user --scope -q -p MemoryMax=2G -p MemorySwapMax=0 <probe-command>
```

Run Rust checks and tests through the required wrapper:

```sh
bash ~/.claude/scripts/direct/verify.sh check style_reader
bash ~/.claude/scripts/direct/verify.sh test style_reader
```

Do not invoke Cargo directly.
The Mac requires its own binary from the Mac build queue.
Verify architecture, source identity, protocol identity, and differential results before enabling Rust there.
The Python path remains available on the Mac until that proof exists.
A natedev executable cannot serve as the Mac installation.

### Phase 12 proposal

Use one day of private shadow comparisons after phase 11.
Include mutation sequences and ordinary production input captures.
Present results and a concrete switch change for user approval.
After approval, retain the Python switch for one further observation day.

The showrunner must confirm this schedule.
This design does not authorize the switch or choose the user's process.
Keep the previous compatible release and configuration for rollback during observation.
Test rollback after cache publication and a normal ticket assignment.
Rollback must not reverse authoritative schema or ticket formats.

After observation, remove obsolete Python selection and private derived-cache consumers.
Retain writer helpers required by other commands.
Audit each old table before removing its triggers or rows.
Do not delete the reference implementation before acceptance completes.

## Risks and first measurements

| Risk | First measurement or proof | Response |
| --- | --- | --- |
| The port preserves repeated algorithms | Count searches, scans, pair comparisons, and result construction | Replace repeated traversal with shared indexes |
| Source rows appear cheap only with cached pages | Repeat projected reads on a private capture; record host state | Report filesystem-cold results separately |
| Git history dominates | Time unique ancestry/diff pairs and object checks | Share revision work and prove native semantics |
| Regex exports or document commands exceed the budget | Measure operations outside the current `COUNT` benchmark | Remove repeated work without semantic changes |
| A source lacks invalidation coverage | Mutate every audited source in fixtures | Refuse warm reuse until its proof passes |
| New declarations evade positive dependencies | Add a previously absent matching declaration | Subscribe to search domains and empty results |
| Warm reuse preserves unavailable evidence | Remove and restore an object between refills | Validate shared object requirements each observation |
| Assignment diverges from `COUNT` | Compare action traces and fault recovery | Preserve mutation order and publication checks |
| Cache payloads repeat source data | Measure bytes by partition and shared field | Intern values and remove redundant fields |
| Concurrent changes prevent stable selection | Inject changes during reads and publication | Bound retries and return a waiting result |
| Compilation enters the timed command | Test an absent or mismatched binary | Return a prerequisite failure; install before deployment |
| Mac locale or Git behavior differs | Run the same corpus with the Mac-built binary | Keep Python until parity passes |

## Questions for the showrunner

1. Confirm the owner of daily Python/Rust integration and parity failures.
2. Confirm capacity for concurrent phases 7 and 8 after phase 6.
3. Confirm the proposed shadow and rollback observation schedule for phase 12.

The user still owns the default switch decision.
Architecture and measurement work can proceed before those process choices are settled.

Answers from the showrunner (2026-11-16):

1. style-unit owns daily Python/Rust parity failures.
2. Phases 7 and 8 run at the same time, one worker each.
3. Phase 12 has two days of shadow after phase 11. Rollback is `reader=python`. The phase 12 switch needs the user's go.
4. Keep the design simple. If a phase finds that the cache or a layer is not necessary for the target, drop it and tell the showrunner.

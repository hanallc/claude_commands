# Hana F1, G, and F2: execution and results

Status: design only. No route, resource, or configuration value changes in this document.

F1 lets an assigned Hana judgment run remotely without holding natedev memory or its cargo token. G links remote work and local fallback, checks results against the requested source, and reports AWS costs. F2 runs the judgment inside its leased copy and keeps its process locks through compilation. Existing local repairs and nextest remain local.

## Current baseline

The director confirms that queue 44 and 44c are live in commit `16a9610e7`.
The `todo` labels in `builds-queue.md` are stale for these dependencies.
This design uses the current code, rather than waiting for another queue merge.

| Contract | Current implementation |
| --- | --- |
| Request and assignment | `admission_wire.PlacementRequest`; `placement.PlacementConstraints` and `PlacementAssignment`. |
| Separate coordinator and holder | `SubmittingOwner`, `ClaimHolderIdentity`, and `ConsumePermission`. |
| One launch authorization | `AdmissionServer._consume_permission`; persisted `FirstAuthorization` versus status-only replay. |
| Reports and settlement | `ExecutionReport`, `AttemptOutcome`, `DispatchPathState`, `_handle_placement`, and `_settle_attempt_reports`. |
| Provider observations | `FreshHostEvidence`, `AdoptedProviderClaim`, `RetainedCleanupClaim`, and `ExecutionEvidence`. |
| Provider notices | `BusyProviderDiagnostic` and `ProviderRetryNotice`. |
| Caller-specific hold policy | `HostEvidenceKey.local_hold_owner`; `ConfirmedCallerLineage` and `UnavailableCallerLineage`. |
| Assigned execution inputs | `build_targets.fixed_placement_constraints`, `reserve_assigned`, and the existing `assigned-info` command. |

`PlacementDestination.provider` already permits `ssh` for a configured Linux target.
That value describes the placement family; it does not require Hana to open SSH.
E must select the Hana backend through the configured target settings.
No branch may compare the target name with a hard-coded `hana-aws` string.

`hana_box.HanaController` already supplies `request`, `claim_status`, `permission`, `release`, and bounded lifecycle recovery.
`hana_spend.HanaSpending` already reserves starts, renewals, and storage under the ledger lock.
`hana_source.materialize` already yields `LeasedSource | MissingBasis` while it holds the maker and copy leases.
`hana_transport.SourceSender.prepare` already captures the source through `copies.capture_source`.
These functions remain the owners of their existing work.

## Boundary with E

E owns configured candidates, capability policy, start claims, provider observations, and placement reservation adapters.
F1 owns command dispatch, execution fencing, settlement, and the remote waiter's execution evidence.
G owns accepted result publication, linked buildlog rows, and cost projections.
F2 owns guest command execution and the copy lock handoff.

E and F1 share the existing `PlacementAssignment`, `ProviderClaimReference`, `ClaimHolderIdentity`, and `hana_box.CommandPermission` values.
Use `placement.assigned_claim_id(episode, attempt)` for the exact provider claim identity.
Use the same `assigned_claim_id` as `WorkClaim.execution`, as E specifies.
Use `placement.attempt_key` only for the existing queue assignment lookup.
Carry the actual deployment, verified boot, and acknowledged permit from D; invent no substitute boot identity.
Keep the submitting coordinator separate from the natedev adapter that holds the claim.
E returns `HanaClaimAdopted`; F1 consumes queue permission once for the resulting dispatch.
The shared sequence must not consume permission independently in both adapters.

E publishes Hana evidence through the existing service background-evidence path.
It must bypass the ordinary SSH probe, remote process scan, and rsync branches for this backend.
Its claim evidence must retain unresolved execution ownership after an adapter dies.
F1 supplies the durable execution state from which E builds that evidence.

The companion E design is [builds-aws-hana-e.md](builds-aws-hana-e.md).
Use its `ExecutionTransport`, `HanaControlTransport`, `HanaClaimAdopted`, and `HanaHostGeneration` types.
E owns `hana_placement.py` and the assignment-bound claim adapter.
The questions below identify the remaining integration decisions.
They are Hana extensions to live queue contracts, rather than missing Phase 44 APIs.

## New values and ownership

All new domain values use frozen dataclasses and named outcome variants.
Decode untyped JSON only at the transport or record boundary.
Use no `Any` and no bare optional domain state.
The names below are proposed implementation names, not claims that code already defines them.

| New value | Fields and meaning | Owner |
| --- | --- | --- |
| `HanaAttemptKey` | `episode`, `attempt`, and `claim_id`: `str`; `generation: HanaHostGeneration` from E. The claim ID also names lifecycle execution. | Shared protocol, `hana_result.py`. |
| `HanaExecutionRequest` | `HanaAttemptKey`, `SourceManifest`, assigned `copy_number: int`, source key `SourceKeyKnown`, argv `tuple[str, ...]`, and `LogicalStepLink`. | F1. |
| `HanaDispatchState` | `DispatchPrepared | DispatchSent | DispatchAcknowledged | DispatchFenced | DispatchSettled`. Each variant carries `key: HanaAttemptKey` and `evidence: placement.ExecutionEvidence`. | F1. |
| `HanaSettlement` | `HanaSettled(outcome, evidence) | HanaSettlementPending(reason)`. Reuse queue outcome and execution-evidence types. | F1. |
| `RemoteStepWaiter` | Exact `PlacementAssignment`, checked `ClaimHolderIdentity`, and retained provider reference. It supports E's `remote_waiter` classification, without local execution permission. | E writes; F1 supplies execution evidence. |
| `LogicalStepLink` | `call_id: str` and `episode: str`; shared by the remote attempt and its permitted local fallback. | G. |
| `HanaCommandResult` | `key: HanaAttemptKey`; source key, commit, toolchain, host: `str`; argv: `tuple[str, ...]`; `copy_number` and status: `int`. | G, `hana_result.py`. |
| Result output fields | stdout and stderr: `bytes`; started and finished times: `float`; execution seconds: `float`; `memory: MeasuredPeak | PeakUnavailable`. | G. |
| Peak variants | `MeasuredPeak(bytes: int)` records measured memory; `PeakUnavailable(reason: str)` records absent evidence. | G. |
| `HanaResultDecision` | `AcceptedHanaResult(result) | RejectedHanaResult(reason)`. Only the accepted variant reaches the recorder or verify result adapter. | G. |
| `HanaAttemptCharge` | `key: HanaAttemptKey`, `link: LogicalStepLink`, byte and request counts: `int`, times: `float`, ledger references: `tuple[str, ...]`. No dollar calculation. | G. |
| `HanaRunOutcome` | `HanaCommandFinished(result) | HanaExecutionRefused(reason) | HanaExecutionLost(reason)`. | F2. |

Keep monetary values as `Decimal` inside the existing ledger.
Use wall-clock epoch seconds for persisted event times and monotonic deadlines for local waits.
Use byte counts as `int`, elapsed seconds as `float`, and paths as `Path`.
Reuse `hana_source.SourceIdentity` and `LeasedSource`; add no second snapshot or copy identity.

Permit generations renew spending permission for an execution.
A renewal must not create another execution or invalidate its accepted result identity.
The assignment attempt and verified boot fence the execution lifetime.
A replacement assignment always has another attempt ID.

## F1: dispatch, fencing, and local release

| File | Function change or addition |
| --- | --- |
| `scripts/aws_target/hana_execution.py`, new | Add `prepare_execution -> HanaExecutionRequest`, `dispatch_execution -> HanaDispatchState`, `observe_execution -> ExecutionEvidence`, and `settle_execution -> HanaSettlement`. Refusal uses the named outcome variants. |
| `scripts/aws_target/hana_provider.py` | Add bounded command-object publication and observation beside `_object`; retain `PROVIDER_OPERATIONS` policy coverage. |
| `scripts/aws_target/hana_box.py` | Bind dispatch and settlement to the current lifecycle claim; keep unresolved starts owned through `release`. |
| `scripts/mac_test/offload.py` | Extend `run_offload`, `claimed_run`, `ProcessController`, and `report_placement_result` for the configured Hana backend. |
| `scripts/build_targets/build_targets.py` | E owns `reserve_assigned` and transport dispatch. F1 supplies its execution requirements; preserve the `assigned-info` output shape. |
| `scripts/build_queue/admission_service.py` | E writes this shared file. F1 supplies settlement changes for `_start_pending`, `_claim_holder_ended`, and provider-release checks. |
| `scripts/build_hold/build_hold.py`, `hold_enforcer.py` | E owns the checked `remote_waiter` classification. F1 supplies evidence and drain rows for `audit_builds`, `classify_drain_runs`, and `decide_builds`. |

Keep `scripts/direct/verify.sh` unchanged.
It already releases the cargo token after remote placement and before the assigned reservation.
Remote assignment already bypasses a local memory grant.
Reuse this path and its prerequisite reporting; add no second token or memory-release protocol.
Local fallback must obtain the queue's `LocalPlacementGranted` after its local token prerequisite becomes ready.

1. Validate the assigned target, scope, copy number, coordinator, and adopted holder through E's reservation adapter.
2. Capture exact source through `SourceSender.prepare` and the existing copies source policy.
3. Bind the captured commit to the assignment's verify source key, exact command, features, and toolchain identity.
4. Recheck the live source key after capture and before consuming permission.
5. Persist the pending dispatch before publishing any executable request.
6. Request `ConsumePermission` with the exact claim and holder.
7. Publish an execution request only after `FirstAuthorization` and current `HanaController.permission` both succeed.
8. Observe execution or settle the same pending dispatch after a lost reply.

A status reply never grants another launch.
A lost authorization response starts no command.
An uncertain execution publication cannot be resent as a new executable request.
Observation and fence publication may retry without repeating command dispatch.

The guest receiver uses one start fence per exact attempt outside the leased source directory.
Its command acceptance and fence check share a lock.
A fence acknowledged before command acceptance prevents a delayed request from starting.
If acceptance wins, settlement requires command-cgroup termination and observed lease release.
Keep this within Phase 44's start-fence semantics; add no separate fallback or replay authority.

Before release, stop local dispatch children and resolve pending publication.
Publish the attempt fence, stop any accepted command, and confirm the lease is free.
Persist the fence before accepting loss as settled.
Then release the exact lifecycle claim and report `DispatchPathSettled`.
A failed fence, missing result, expired URL, or failed observation remains `DispatchPathPending`.
A timeout, dead owner, empty scan, or vanished local process alone proves no remote departure.

Use `PrelaunchRefused` only when no command started and its dispatch path is closed.
Use `AttemptDeclined` for a settled lost or invalidated remote attempt that permits queue retry.
Use `AttemptCompleted(status)` for a valid compiler result, including nonzero status.
Use `AttemptCancelled` for user cancellation; it does not silently launch local work.
Use `AttemptOutcomeUnavailable` while execution or cleanup remains uncertain.

The queue alone creates the replacement attempt and selects permitted fallback.
Its episode retains its original waiting time and priority.
Fallback uses the current live worktree; it never installs the remote snapshot locally.
Retain provider exclusion while cleanup remains incomplete.

The local waiter keeps owner and hold records while remote work runs.
A natedev drain preserves that checked waiter without a local memory charge or cargo token.
An environment variable alone cannot grant the waiter classification.
Provider hold or cancellation still stops remote work through settlement.
Keep the controller-lock-before-ledger-lock order, and hold neither lock during network waits.

## G: checked results, linked rows, and costs

| File | Function change or addition |
| --- | --- |
| `scripts/aws_target/hana_execution.py` | Decode `HanaCommandResult`; add `accept_result` returning `HanaResultDecision` under the attempt journal lock. |
| `scripts/mac_test/offload.py` | Pass accepted results through the existing result adapter and `persist_attempt`; preserve diagnostics path mapping. |
| `scripts/aws_target/hana_result.py`, new | Define the shared attempt/result records and `validate_result -> HanaResultDecision`. Keep provider and credential imports outside this guest-safe module. |
| `scripts/buildlog/record.py` | Extend the step and attempt writers with link, attempt, source, and terminal-role fields. Add no cost calculation. |
| `scripts/buildlog/index.py` | Extend `STEP_COLUMNS`, attempt columns, and `add_step`; bump `SCHEMA_VERSION` from its implementation baseline. |
| `scripts/buildlog/report.py`, `memory.py`, `rebuilds.py` | Use explicit execution hosts and linked-step counting in the existing report queries. |
| `scripts/aws_target/ledger.py` | Add durable attempt links and ledger-backed kept-machine spend grouping; preserve existing Hana exposure accounting. |
| `scripts/buildlog/aws_report.py` | Extend `daily_spend_lines` and related projections with kept-machine compute and retained storage. |

Check the deployment, boot, current attempt, exact claim, copy number, source key, commit, toolchain, and command before publishing a result.
Check the caller's current source key again after result transfer.
The captured commit and verify source key have different meanings; record their binding rather than treating them as interchangeable hashes.
A fenced, replaced, mismatched, or late result writes no buildlog row and creates no verify cache entry.
A source mismatch can produce a separate settled-loss event; the rejected result itself remains unpublished.
A valid compiler failure remains terminal and keeps the existing clippy warning policy.

Persist one accepted terminal decision per attempt before publishing its deterministic record ID.
Use the recorder's existing pending-row and `append_row_once` path for crash recovery.
Replayed delivery publishes the same row, rather than another final result.
Keep stdout, stderr, exit status, execution time, peak memory, and diagnostic paths in the result transport.
Return no patch or binary.

Proposed nullable index fields are `step_link`, `placement_episode`, `placement_attempt`, `execution_generation`, `source_key`, `attempt_role`, and `is_final_result`.
Use `attempt_role` values `remote` or `fallback` at the record boundary.
Record final-result selection separately, so a valid remote result can be final without losing its execution role.
Older rows have SQL `NULL` for absent fields.
Preserve every existing column's meaning and the permanent JSON lines.
Rebuild only the derived index when the schema changes.

A lost remote attempt and local fallback have separate step rows and the same `LogicalStepLink`.
Each row records the host that executed it; the link does not overwrite that host.
Count one logical result from the accepted final row.
Show remote lost time separately; keep natedev memory pressure, Rebuilds, and step totals limited to natedev execution rows.
The link must include the episode because one verify call can contain several steps.

Reuse `hana_compute`, `hana_storage`, `hana_lifecycle_fence`, and `hana_intervals` from Phase C.
Do not charge compute separately for each overlapping permit renewal or linked attempt.
Keep idle and setup compute visible even when no step owns that interval.
Do not release compute exposure at command completion; retain it until physical stop confirmation.
Keep storage exposure until confirmed deletion.
Use bills when available and label estimates and revised bills through the existing ledger policy.
Add the kept-machine compute column beside the existing 32-CPU and 16-CPU columns.
Transfer and query events link to attempts without creating another source of dollar totals.

## F2: the leased command and image

| File | Function change or addition |
| --- | --- |
| `scripts/aws_target/hana_receiver.py` | Accept fenced command envelopes and publish command observations beside permit acknowledgements. |
| `scripts/aws_target/hana_runner.py`, new | Add guest `materialize_and_run(request) -> HanaRunOutcome`. Receiver supervision owns root operations. |
| `scripts/aws_target/hana_source.py` | Extend `materialize` with a request for the assigned copy through `build_copies.take_copy`. |
| `scripts/mac_test/linux_runner.py` | Use `run_copy` and prepare the shell with the maker copy's locks and Cargo environment. |
| `scripts/lib/build_copies.py`, `build_folders.py` | Acquire the assigned copy with `ExactCopyRequest`. |
| `scripts/aws_target/hana_render.py`, `hana_render.files` | Render the execution entry point and its complete import closure with the safety and receiver files. |
| `/etc/nixos/hosts/aws-hana.nix` | Add `git`, `curl`, the execution, source, and copy modules, and the existing shared Hana toolchain environment. |

The NixOS changes belong to the natedev owner's branch and review.
The current image has Python and safety files, but it does not include all imports for source preparation and copy leases.
Include transitive modules such as copies, configuration readers, maker, rescue, and copy wait support.
Test that closure in isolation; copying only `hana_source.py` cannot work.

Use `hana_guest.admit_command` after checking the actual data mount and current execution permission.
Keep source preparation, shell preparation, and Cargo inside its bounded `hana-work.slice` command cgroup.
Run these processes as `hana-runner`, outside the root receiver.
Keep permits, fence authority, and controller files root-only.
Give the runner only its allowed source manifest and scoped input access.
Root supervises output publication without exposing controller credentials to build scripts.

Acquire and materialize the copy inside the systemd command unit.
Do not acquire its process lock before `systemd-run` and assume the service manager inherits that descriptor.
Keep the `LeasedSource` context open through shell preparation, Cargo, recording, and result capture.
Pass the held maker descriptors to source, shell-preparation, and Cargo subprocesses through `pass_fds`.
Reuse `floor_claims.inherited_mirror_descriptors` for the mirror locks.
Extend that handoff with the actual `build_folder_lock_fd` values.
A child that outlives its wrapper must retain the lease until that child ends.

Preserve the copy's `cargo_dir` and environment.
The current Linux runner assumes an output directory below its mirror.
Use `LeasedCopyExecution` to pass the maker Cargo directory and preserve the mirror path checks.
Keep the standard SSH and Mac execution paths unchanged.

Acquire the assigned copy with `ExactCopyRequest`.
Refuse or wait through admission if the copy is unavailable.
Use the existing allocator and its copy limit.
Preserve affinity, stable paths, rescue before reset, and the limit on total copy size.
Set the Hana copy limits with `hana.copies.max` and `hana.copies.max_bytes` in `config/aws_target.conf`.

Use the source manifest's exact argv and allowed environment.
Check compiler and shell parity before Cargo starts.
On `MissingBasis`, use `SourceSender.retry_complete` for the same captured commit.
That source transfer retry is not another command authorization.
Keep the watchdog independent of source, tools, and controller availability.
After wrapper death or controller loss, only cgroup termination and lease release establish departure.

## Work that can proceed in parallel now

| Work package | Can build now? | Merge or activation dependency |
| --- | --- | --- |
| E capability rules and fake provider evidence | Yes; use the live Phase 44 types. | Agree the shared claim and generation mapping with F1. |
| E live start/reservation adapter | Yes, in a separate file from F1 dispatch. | E and F1 integration rows before routing; E owns shared file edits. |
| F1 journal, start fence, and settlement tests | Yes; use fake E observations and current queue reports. | E claim evidence and exact reservation integration. |
| F1 local release and drain waiter | Yes; supply execution evidence and rows to E's hold writer. | Resources and queue review of the waiter evidence. |
| G result types, validators, schema fixtures, and ledger projections | Yes, parallel with E and F1. | F1 accepted-decision journal before live publication. |
| G report queries and ledger tests | Yes; use fixture rows and bills. | Report owner's schema and counting agreement. |
| F2 image import closure and fake execution tests | Yes, parallel with E, F1, and G. | Natedev review and shared copy API. |
| F2 copy execution integration | Partly; the source and safety APIs exist. | Build folders Phase 4 copy selection and lock contract; E and F1 dispatch boundary. |
| Full accepted result and local fallback proof | No isolated proof without integration. | E, F1, G, and F2 combined. |
| Live activation and spending | No. | Phase H, owner agreements, approved copy capacity, USD 6 first-day bound, and user `/tune`. |

No package waits for queue 44 or 44c to merge again.
F1 owns `hana_execution.py`; G owns `hana_result.py` and report/index changes.
F2 owns `hana_runner.py`, guest receiver changes, and the image proposal.
E owns target, hold, and admission-service integration.
F1 owns `offload.py`; F2 owns `linux_runner.py` after verify agreement.
Serialize edits to `hana_receiver.py`, `hana_box.py`, `offload.py`, and `admission_service.py` at their shared boundaries.
E owns the service provider-evidence edit; F1 supplies its settlement evidence contract.
Keep the inactive target row outside `targets=` and leave all current configuration values unchanged.

## Required test rows

Add rows beside the existing tests; change no existing Mac, SSH-target, or verify assertions.
Use fake providers, clocks, process roots, private records, and maker-created test repositories.
No test needs AWS, a live unit, a real hold, or live claims.

| Phase | Required row and proof | Test home |
| --- | --- | --- |
| F1 | Drain preserves only the checked remote waiter; it holds no local memory reservation or cargo token. | New rows beside `test_hold_enforcer.py`, placement tests, and verify offload tests. |
| F1 | Pending claim, pending start, lost authorization, and lost start acknowledgement block overlapping fallback. | `test_hana_execution.py`, offload placement tests, and `test_admission_service.py`. |
| F1 | Settled prelaunch refusal closes immediately; uncertain cleanup retains the provider claim and exclusion. | Execution and service report rows. |
| F1 | A delayed command cannot start after its exact fence acknowledgement; duplicate permission returns status only. | Receiver and execution race rows. |
| F1 | Caller death, cancellation, interruption, and controller restart retain or settle the same attempt exactly once. | Execution recovery and lifecycle rows. |
| F1/G | Local edits before fallback recapture current source; no remote snapshot replaces the local tree. | Offload and source tests. |
| G | Edits during execution or transfer reject the remote source or command mismatch. | Result validator rows. |
| G | A late result from a fenced attempt creates no row or verify cache entry. | Recorder and offload result rows. |
| G | Delivery replay and recorder restart preserve one accepted final result. | Recorder pending-row and index rows. |
| G | Linked remote loss and local fallback count once; each keeps its host, time, memory, and Rebuilds scope. | Index and report rows. |
| G | Old records produce NULL fields; older column meanings remain unchanged. | `test_index.py` and report compatibility rows. |
| G | Renewals, stop intervals, idle time, delayed bills, and revised bills charge physical compute once and retain storage. | `test_ledger.py`, `test_hana_spend.py`, and AWS report rows. |
| F2 | Two dirty worktrees, deletions, staged changes, and allowed untracked source retain distinct identities and the real index. | Existing source/copies tests plus execution rows. |
| F2 | A full pool or unavailable assigned copy prevents allocation. Affinity and paths stay stable. | Source and copy lease tests. |
| F2 | A wrapper dies while its child holds the lease; reset stays blocked until the child exits. | Execution and copy lease tests. |
| F2 | Rescue failure retains source; an oversized copy loses its Cargo output once after verified rescue. | Source and copy lease tests. |
| F2 | Compiler mismatch, incompatible shell, or missing disk refuses Cargo; failed tools do not disable the guard. | Linux runner and guest rows. |
| F2 | Expired permits kill the command cgroup and stop independently of the controller, including during source or shell preparation. | Guest watchdog and execution rows. |
| E/F1 | Wrong repository, repair flags, disabled mode, and explicit pins preserve policy; simultaneous demand starts produce one claim. | E provider rows with F1 integration assertions. |

Send queue the queue/copies diffs, verify the offload/runner diffs, and report the recorder/report diffs before checkpoint and merge.
Resolve the pending `ConnectTimeout` edit with verify before changing shared files.
Keep D9 review cleanup and D8 alarm-only sweeping as later acceptance, as the parent plan requires.

## Questions for the queue owner

| Question | Recommended answer | Status |
| --- | --- | --- |
| Q1. Can Hana evidence retain `provider="ssh"` while its transport uses S3? | Yes. Select the backend from target settings and bypass SSH probes within the existing evidence adapter. | Hana extension; Phase 44 types are settled. |
| Q2. How does settlement override the dead-holder shortcut in `_provider_claim_released`? | Keep durable pending dispatch visible to `_start_pending` until guest fencing and lease release settle it. Never close from holder death alone. | Agree with E before integration. |
| Q3. Must accepted terminal reports prove guest settlement before `DispatchPathSettled`? | Yes. `_settle_attempt_reports` trusts that state and can close without another provider observation. F1 must supply the proof first. | Existing queue behavior; add Hana integration rows. |
| Q4. Which Phase 4 API acquires the assigned maker copy? | Use `build_copies.take_copy` with `ExactCopyRequest`. Keep allocation, rescue, and locks in the shared API. | Build folders API decision, not a Phase 44 wait. |
| Q5. How should renewal generations appear in provider evidence? | Use E's length-framed `HanaHostGeneration` hash for adoption and the fixed assignment for execution. Keep permit generations separate. | Matches E; queue confirms its projection. |
| Q6. What creates the logical-step link for fallback? | Use the existing call ID plus placement episode. Preserve it through queue-created replacement attempts without changing `verify.sh`. | Confirm assigned result and buildlog fields with queue and report. |
| Q7. Can the checked waiter remain through a drain after its dispatch becomes uncertain? | Yes, while its retained provider claim proves unresolved remote ownership. Retain no local execution grant or token. | Resources and queue confirm the classification. |

No open question above requires another selector, queue, snapshot protocol, or spending authority.

## Code estimate

These are engineering estimates, not measured execution times.
Queue 44 and 44c already satisfy the original queue-merge prerequisite.

| Package | Hours |
| --- | ---: |
| F1 dispatch, fence, recovery, local release, and hold rows | 8–12 |
| G result validation, recorder/index compatibility, linked reports, and ledger projections | 7–11 |
| F2 execution in the assigned copy, descriptor handoff, image closure, and guest tests | 8–12 |
| Combined placement, fallback, restart tests, owner review corrections | 4–6 |
| Total code and checks | 27–41 |

With three writers, the estimated elapsed implementation time is 14–22 hours after the shared interface decisions.
The estimate excludes waiting for the shared copy API, NixOS review, image build, owner approvals, and the first-day trial.
A new queue wire format or stored-state conversion needs a separate estimate and the existing deploy conversion procedure.

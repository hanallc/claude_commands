# Hana phase E: placement through the configured target

Status: implementation design, 2026-11-16. No code or configuration changes form part of this document.

## 1. User outcome

Hana check runs and clippy judgments can use the kept AWS machine before natedev when configuration permits that route. Repairs, builds, and nextest remain local. A busy or unavailable machine permits local execution only after the previous remote attempt settles. Existing pins and other repositories retain their placement behavior. Placement cannot provision resources or enable spending without the required approval.

## 2. Baseline and existing boundaries

Queue phases 44 and 44c are already on main and live at `16a9610e7`.
The director confirmed this baseline during this design.
Their `todo` labels in [builds-queue.md](builds-queue.md) are stale.
Nothing in this design waits for a queue phase to merge.
The implementation must use the installed code rather than reconstruct the earlier plan.

| Live boundary | Contract used by E |
| --- | --- |
| `target_config.read_targets()` | The sole reader of registered target rows and configured order. |
| `build_targets.fixed_placement_constraints()` | Constructs candidates without saving temporary resource exclusions. |
| `build_targets.step_routing_rule()` | Preserves local writes, pins, and final-step rules. |
| `eligibility.repository_identity()` | Supplies `KnownRepository.key`, including normalized origin identity. |
| `eligibility.linux_kind_permitted()` | Applies the existing Linux command capability keys. |
| `admission_wire.PlacementRequest` | Carries the command, profile, source, coordinator, session, local context, and prerequisites. |
| `AdmissionServer._handle_placement()` | Receives requests, claim failures, cancellations, and execution reports. |
| `AdmissionServer._placement_pass()` | Uses the existing queue order and local admission preview. |
| `AdmissionServer._consume_permission()` | Persists exact adoption and consumption before returning `FirstAuthorization`. |
| `HostEvidenceWorker` and `HostEvidenceReader` | Read bounded provider evidence outside the admission loop. |
| `placement.PlacementAssignment` | Binds the episode, attempt, destination, source, coordinator, and session. |
| `admission_wire.ExecutionReport` | Carries typed execution evidence, outcome, and dispatch settlement. |
| `build_targets.read_caller_lineage()` | Uses the checked requesting process for local hold decisions. |

`PlacementRequest` is defined in `admission_wire.py` and exported through `admission_protocol.py`.
`admission_service.py` uses it through `wire`.

The configured target remains `kind=ssh`, `platform=linux`.
That row selects the existing remote-target placement path.
It does not require an SSH connection to the Hana guest.
The guest retains its existing signed S3 transport and receives no SSH key or instance credentials.
`placement.PlacementDestination.provider` remains `ssh`; E adds no provider literal or independent selector.

Current Linux target code assumes SSH connection settings in several places.
The Hana workload file does not contain those settings.
The integration must select the transport before calling `eligibility.read_config()`, SSH probes, or mirror cleanup.
A missing SSH setting must never become a fake host or timeout value.

## 3. Configuration and semantic values

The later implementation adds only inactive configuration.
It leaves `enabled=off` and the existing `targets=` value unchanged.
The target name occurs in configuration and fixtures, never in production selection conditions.

| File | Proposed new entries |
| --- | --- |
| `config/build_targets.conf` | `kind.hana-aws=ssh`, `platform.hana-aws=linux`, `settings.hana-aws=hana_aws.conf`. |
| `config/hana_aws.conf` | `execution_transport=hana-control`, `host=aws-hana`, `repository_identity=https://github.com/hanallc/hana`, `kinds=check,lint`. |
| `config/hana_aws.conf` | `max_steps=1` and `free_floor_gib=30`, as required by the parent plan. |

`host` is a reporting alias, not a network address.
`max_steps` supplies the one-step claim limit; E introduces no second concurrency setting.
The target advertises one candidate slot through the existing slot-capacity boundary.
The guest still requires separately approved maker capacity.
Set the Hana copy limits with `hana.copies.max` and `hana.copies.max_bytes` in `config/aws_target.conf`.

The user must approve active target ordering through `/tune` after approving the USD 6 trial bound.
The recommended order places the Hana target before natedev and preserves every other target's relative order.
E neither appends a hidden target nor reorders candidates in code.

All proposed records below are frozen domain records unless the table identifies an existing type.
`records` means `admission_records`; `wire` means `admission_protocol`.
No new domain field uses a bare optional value.
Transport decoding converts missing or malformed data into named unavailable variants.

| Value or type | Fields and meaning |
| --- | --- |
| `SshCommandTransport` | `settings: Path`; the existing SSH command path. |
| `HanaControlTransport` | `settings: Path`, `host: str`; signed control with a safe host alias. |
| `ExecutionTransport` | `SshCommandTransport | HanaControlTransport`. Missing legacy transport configuration selects `SshCommandTransport`. |
| `DisabledHanaRoute` | Placement is disabled by configuration. It grants no claim or provider mutation. |
| `EnabledHanaRoute` | `repository_key: str`, `kinds: tuple[placement.CommandKind, ...]`, `max_steps: int`, `free_floor_gib: int`. |
| `HanaRouteSettings` | `DisabledHanaRoute | EnabledHanaRoute`. An enabled route requires `max_steps == 1`. |
| `HanaCheck` | `command: tuple[str, ...]`, `recorded_step: str`; a validated check judgment. |
| `HanaClippyJudgment` | `command: tuple[str, ...]`, `recorded_step: str`; a validated clippy judgment without repair flags. |
| `HanaStepRefused` | `reason: str`; the command is outside the fixed workload. |
| `HanaStepPolicy` | `HanaCheck | HanaClippyJudgment | HanaStepRefused`. |
| `HanaHostGeneration` | `deployment: str`, `start_token: str`, `boot_id: str`; one verified boot, independent of permit renewal. |
| `HanaLifecycleEvidence` | `deployment: hana_box.DeploymentIdentity`, `state: hana_box.Lifecycle`, `boot: VerifiedBoot | BootUnverified`, `operation: PendingOperation | NoOperation`. |
| Remaining `HanaLifecycleEvidence` fields | `permit: DeliveredPermit | NoDeliveredPermit`, `claims: tuple[WorkClaim, ...]`, `observed_at: int`; all component types come from `hana_box`. |
| `HanaUnprovisioned` | No approved deployment exists. This is a definite state rather than unreadable lifecycle evidence. |
| `HanaLifecycleUnavailable` | `reason: str`, `observed_at: int`; no claim absence or readiness can be inferred. |
| `StartAffordable` | `expires_at: int`; the current projected spending check permits a demand start, without reserving or granting permission. |
| `HanaClaimPending` | `reference: placement.ProviderClaimReference`, `claim: hana_box.WorkClaim`, `deadline: int`. A lifecycle start still owns capacity. |
| `HanaClaimAdopted` | `reserved: placement.ReservedHost`, `generation: HanaHostGeneration`. This claim can request queue authorization. |
| `HanaClaimRefused` | `reason: str`; a definite refusal before a command starts. |
| `HanaClaimRetained` | `reference: placement.ProviderClaimReference`, `reason: str`; recovery or cleanup still owns capacity. |

Use the existing `SubmittingOwner`, `ClaimHolderIdentity`, `SubmittingSession`, and `ProviderClaimReference` without replacing their meanings.
The local coordinator and local claim holder remain distinct processes.
The guest PID belongs only to remote execution evidence.

Use `placement.assigned_claim_id(episode, attempt)` as `WorkClaim.execution`.
The existing target claim stores already hold the episode, attempt, scope, holder, and session.
The lifecycle journal already stores the execution key.
These records provide the join; E adds no parallel claim journal or lifecycle service.

Serialize `HanaHostGeneration` into the existing generation string with stable, length-framed hashing.
Permit renewal does not change that string.
A new boot or demand-start challenge changes it.
Never use the increasing spending permit number as the queue host generation.

## 4. Files and functions

These are proposed implementation edits, not changes made by this design dispatch.

Implementation keeps the public claim boundary in `hana_placement.py` and puts backend logic in `hana_placement_adapter.py`.
`execution_transport.py` defines the canonical transport records; `hana_placement.py` exports the same records.
`hana_generation.py` defines the canonical boot generation without importing controller code.
`hana_placement.py` exports that same generation for F1.
`HanaController.inspect()` owns the immutable lifecycle projection types in `hana_box.py`.
These boundaries prevent import cycles through the ledger and guest result codecs.

| File | Functions or changes | New result types |
| --- | --- | --- |
| `scripts/build_targets/target_config.py` | Add `read_execution_transport(target: SshTarget)`. Reject unknown transport values and incompatible platforms. Keep `read_targets()` as the only row reader. | `ExecutionTransport`. |
| `scripts/build_targets/build_targets.py` | Extend `fixed_placement_constraints()`, `ssh_target_eligibility()`, `reserve_assigned()`, and status dispatch for the selected transport. | Existing `PlacementConstraints`, `EligibleTarget`, `ExcludedTarget`; typed Hana claim results at the adapter boundary. |
| `scripts/mac_test/eligibility.py` | Reuse `repository_identity()` and `linux_kind_permitted()`. Keep legacy `OffloadConfig` limited to SSH transport. | Existing repository and capability types. |
| `scripts/aws_target/hana_placement.py` | Add `read_settings(configuration: conf.ConfigRead)` and `classify_step(profile: records.StepProfile | records.ProgramProfile, constraints: placement.PlacementConstraints)`. | `HanaRouteSettings`, `HanaStepPolicy`. |
| `scripts/aws_target/hana_placement.py` | Add `read_evidence(key: placement.HostEvidenceKey, request: wire.PlacementRequest)`. | Existing `placement.HostEvidence`. |
| `scripts/aws_target/hana_placement.py` | Add `claim_assigned(assignment: PlacementAssignment, holder: ClaimHolderIdentity, controller: HanaController)` and exact-identity inspection after a lost claim response. | `HanaClaimPending | HanaClaimAdopted | HanaClaimRefused | HanaClaimRetained`. |
| `scripts/aws_target/hana_box.py` | Add public `HanaController.inspect()` with a locked, immutable projection. Do not expose mutable `BoxRecord` or call private `_read()` externally. | `HanaLifecycleEvidence | HanaUnprovisioned | HanaLifecycleUnavailable`. |
| `scripts/aws_target/hana_spend.py` | Add `HanaSpending.preview_start(now: int, expires: int)`. Share the existing projected-exposure calculation with `reserve_start()`. | `StartAffordable | PermitRefused`. |
| `scripts/aws_target/hana_provider.py` | Preserve `AwsHanaProvider.step()`, `recover()`, scoped calls, and saved mutation tokens. E requires no separate start method. | Existing `ProviderReply` and `MutationRecovery`. |
| `scripts/build_queue/admission_service.py` | Dispatch `_host_probe_timeout()` and `_read_host_evidence()` by transport before any SSH parsing. Extend `_placement_reply()` notice projection. Set new read-visible fields on the loop thread. | Existing evidence and notice types. |
| `scripts/build_queue/admission_code.py` | Include the new imported service module in the service code identity. | No new domain value. |
| `scripts/build_hold/build_hold.py` | Extend `BuildClass` and `audit_builds()` for a verified remote waiter. | The `remote_waiter` classification. |
| `scripts/build_hold/hold_enforcer.py` | Extend `remote_step_callers()`, `claimed_processes()`, `classify_drain_runs()`, and `decide_builds()` for that classification. | Existing process identity and unreadable-claim variants. |
| Tests beside these files | Add the E rows below without changing existing routing assertions. | Private configurations, fake providers, and private process evidence. |

`preview_start()` reads the shared ledger under its existing lock.
It does not reserve exposure, mutate the ledger, publish a permit, or call AWS.
The actual start still calls the existing spending methods under the controller's lifecycle rules.
A favorable preview is evidence, not spending permission.

The Hana bridge contains provider-specific projections, not another placement algorithm.
The admission service retains `_queue_order()`, `_placement_pass()`, `_consume_permission()`, `_handle_placement()`, and local planner ownership.
Use lazy boundary imports only where the existing architecture requires them; do not copy placement types into AWS modules.

Set every new field shown by a queue read on the loop thread so the snapshot is marked dirty.
This rule covers `View`, `Mine`, `Explain`, `BuildHistory`, and `PlacementStatus`.
Background workers return evidence to the loop thread rather than changing these fields directly.

## 5. Candidate, claim, and fallback sequence

1. Read registered targets through `target_config.read_targets()`.
2. Construct candidates through `fixed_placement_constraints()` with the actual command and source identity.
3. Validate the normalized repository key and the existing Linux capability keys.
4. Reject unsupported Hana step semantics before requesting a lifecycle claim.
5. Request placement through `admission_client.request_placement()`.
6. Claim only the assigned target, repository scope, and copy number.
7. Commit incoming work through `HanaController.request()` with the exact assignment execution key.
8. Wait through the existing controller lifecycle until readiness or the configured start deadline.
9. Adopt the exact claim with the checked local holder, submitting session, and verified boot generation.
10. Consume queue permission through `admission_client.consume_permission()`.
11. Execute through F1 and F2 only after `FirstAuthorization` and current `HanaController.permission()` both succeed.
12. Report execution and cleanup through `admission_client.report_execution()`.
13. Request another placement only after the original dispatch path settles.

The repository check compares `KnownRepository.key` with the configured normalized origin.
A directory named `hana` with another origin remains ineligible.
A renamed checkout of the approved repository remains eligible.
The transport namespace from phase B identifies source storage; it does not replace repository eligibility.

The workload accepts check and clippy judgment commands through the existing command construction path.
Inspect the original profile and the actual argument vector.
Reject mend, `--fix`, tree-writing flags, nextest, builds, artifacts, `final`, and unknown command wrappers.
A `lint` label alone does not prove a clippy judgment.
A check subprocess inside a mend step remains local.
F1 repeats this validation at dispatch, including for explicit target pins.
A local repair must finish before the next judgment captures fresh source through phase B.

| Lifecycle evidence | Placement behavior |
| --- | --- |
| Disabled or unprovisioned | Exclude Hana without mutation. Use the normal permitted target path. |
| Idle-stopped and affordable | Permit an assignment to claim incoming work. The controller alone reserves and starts. |
| Ready and idle | Permit one assignment; require current boot and permit before execution. |
| Busy, held, interrupted, unhealthy, or replacement-pending | Return refreshed exclusion evidence. Do not manually restart an interrupted box. |
| Starting for this assignment | Retain its exact claim through readiness or settlement. Bound the claimant wait at `start_timeout_seconds`. |
| Starting for another assignment | Report busy evidence; admit no second step. |
| Timed-out or unacknowledged mutation | Retain the claim and pending dispatch state until the original operation settles. |
| Unavailable or stale evidence | Return the existing unavailable or stale variant. Infer neither readiness nor departure. |

Start and idle-stop serialization remains in `HanaController` under `controller_lock`.
A committed incoming claim prevents idle stop.
A saved stop intent prevents a later start claim.
Two requests may observe an idle box, but only one controller claim can succeed.
There is no overflow copy or second instance.

Provider evidence runs in the existing background worker with the configured bounded call timeout.
Use `aws_timeout_s` for Hana rather than adding unused SSH timeout keys.
An evidence read may inspect records and spending, but cannot claim, start, stop, or clean up work.
Keep provider calls outside the admission loop and outside controller and ledger locks.
Use the journal's `observed_at` for lifecycle freshness.
A new file read cannot make an old AWS observation fresh.
Never publish an empty claim set when a journal or target claim record is unreadable.

Map actual contention to `placement.BusyProviderDiagnostic`.
Use the existing `PlacementWait.retry_notices` and `placement.ProviderRetryNotice` for starting, held, and recovery delays.
The delay comes from the existing polling or retry policy, not a new timeout constant.
The current service constructs retry notices mainly from closed attempts.
E extends that projection for Hana lifecycle waits without adding another message format.

A ready lifecycle state proves no command departure.
F1 supplies command and pending-dispatch settlement evidence before release or fallback.
`HanaController.release()` returning true is not sufficient evidence that a guest command ended.
Likewise, a dead local holder is not sufficient evidence while a Hana start or command remains unresolved.
F1 must preserve this rule in `_start_pending()`, `_claim_holder_ended()`, and provider evidence recovery.

Use `RetainedCleanupClaim` for unresolved recovery ownership.
Construct `AdoptedProviderClaim` only after exact holder, session, claim, and boot checks succeed.
The existing `_consume_permission()` then verifies the claim generation against `FreshHostEvidence.generation`.
Return the same boot generation in both values.

A lost authorization response starts no command.
A repeated consumption returns status only, including after restart.
An uncertain start is inspected under its original identity and is never resent as a new launch.
A definite prelaunch refusal uses `PrelaunchRefused` and `ExecutionNotStarted`.
A failed boot must produce a terminal `ExecutionReport` through the live report API.
The assignment retains its live retry deadline during the boot and failure report.
The configured start wait does not replace or extend that deadline.
Release capacity only after the pending-operation barrier settles.
Permit fallback only after that settlement and within the live retry deadline.
A cleanup-pending refusal also retains its provider reference until cleanup completes.
A lost remote result remains unavailable until F1 fences its execution generation and confirms settlement.

A compiler failure produces `AttemptCompleted(status)` and remains the final command result.
A permitted local retry uses a new attempt within the same episode and queue position.
It uses the live local worktree, not the earlier remote snapshot.
The existing local planner grants and charges local memory once.

## 6. Local waiter and owner boundaries

Remote selection retains the caller's hold and ownership evidence.
It releases unused local continuation admission through the existing placement path.
F1 releases the cargo token through the existing remote offload handoff.
E adds no token or memory release mechanism.
`scripts/direct/verify.sh` remains unchanged for the Hana integration.

Classify a local waiter from exact assignment and checked local process evidence.
Do not infer remote ownership from a command name, target alias, or session alone.
A natedev drain preserves a waiter attached to a committed remote attempt without charging remote memory locally.
An unassigned waiter retains the existing drain behavior.
Unknown ownership never grants a new launch or local hold exemption.
Keep phase 44c's checked caller lineage and process-specific local hold evidence unchanged.

The queue owner reviews the queue adapter and claim boundary before checkpoint and merge.
The resources owner reviews registration, capability keys, and hold classification.
The verify owner reviews F1 and F2 changes in `offload.py` and `linux_runner.py`.
The report owner reviews phase G schema and ledger consumers.
The natedev owner reviews guest image and shared toolchain changes.
These reviews are ownership conditions, not missing queue-phase dependencies.

## 7. Parallel implementation now

All four work areas can begin now against the live queue API.
Completion dependencies remain between their interfaces and integration tests.
No area can activate the route alone.

| Area | Work that can proceed independently now | Integration boundary |
| --- | --- | --- |
| E | Target transport types, inactive registration, capability checks, lifecycle projection, spending preview, background evidence, notices, and hold classification. | Owns target and admission-service edits. Supplies assignment-bound claim results to F1. |
| F1 | Extend `ProcessController`, pending operation tracking, `claimed_run()`, transport selection in `run_offload()`, and `report_placement_result()`. | Uses E transport and claim results. Keeps exact adoption, first authorization, local admission release, and settlement. |
| G | Define linked attempt rows, compatible schema fields, source and command validation, host separation, and ledger cost readers. | Uses existing episode and attempt identities. Wires the final F1/F2 result after those adapters exist. |
| F2 | Build guest tool setup, command execution in a leased copy, mount and toolchain checks, cgroups, bounded command transport, and result delivery. | Uses phase B's `hana_source.materialize()` and the agreed F1 command identity. Activation needs F1 settlement. |

F2 must retain the maker copy lock in the command and its descendants.
It must use `hana_guest.admit_command()` and the independent watchdog.
It must use the shared NixOS toolchain and source-derived compiler identity.
G computes costs only in the AWS ledger report path, never in `record.py`.
G rejects fenced, stale-source, or changed-command results before writing a result row or verify cache entry.

Separate writers can own E, G, and the guest-only F2 files.
F1 owns `offload.py`; F2 gives that writer its controller integration requirements.
Do not assign two writers to `offload.py`, `linux_runner.py`, or the same admission-service functions.
Agree the typed claim and result boundaries before those integration edits.
The director assigns the merge owner for shared files.
This table describes possible work division; this design dispatch starts no workers.

No queue wire or saved assignment shape change is required by the proposed E design.
Existing target claim records and lifecycle records retain their current shapes.
Stored shapes added: none.
If implementation requires a stored shape change, define an external conversion and stopped-writer deployment before merging it.
Do not add compatibility guesses to strict readers.
Add the service module to `admission_code.py` before deployment.
Activation still requires owner agreement, F1/G/F2 acceptance, approved copy capacity, and the user's USD 6 first-day approval.

## 8. Required E test rows

Use private roots, private configuration, fake provider calls, private process evidence, and real fixture sockets where required.
Create test repositories through the existing maker and copy fixtures.
Contact no live queue service, AWS account, or unit.
Keep existing Mac, SSH, and verify routing assertions unchanged.

| Row | Parent-plan requirement | E proof and shared boundary |
| --- | --- | --- |
| E1 | Wrong repository; disabled mode | Wrong-origin `hana` and disabled configuration produce no provider calls. A renamed approved checkout retains eligibility. |
| E2 | Mend, build, nextest, local pin | Preserve placement for every excluded workload. Keep explicit local and non-Hana target pins unchanged. |
| E3 | Clippy repair flags | Reject `--fix`, mend profiles, and unknown command wrappers before a lifecycle claim, including a pinned Hana target. |
| E4 | Local repair followed by judgment | The judgment uses the updated source key. Phase B supplies capture; F1 proves its dispatch identity. |
| E5 | Simultaneous requests and busy machine | One controller claim wins across two dirty worktrees. Publish busy diagnostics for the other request. |
| E6 | Idle-stop race | A committed incoming claim prevents stop. A saved stop intent prevents a new claim until settlement. |
| E7 | Pending or unacknowledged start | The 120-second configured wait ends without releasing uncertain capacity. F1 proves no overlapping local execution. |
| E8 | Settled refusal and cleanup | Close the attempt through the live report API. Keep its provider excluded until fresh evidence confirms cleanup. |
| E9 | Natedev drain during remote execution | Keep the exact remote waiter without a local memory reservation or cargo token. Preserve unrelated waiting-run behavior. |
| E10 | Memory, disk, workload budget, account cap | Exclude with a safe reason. A spending preview mutates nothing. An actual start rechecks the current ledger. |
| E11 | Interruption, restart, cancellation | Exclude interrupted capacity and retain uncertain claims. F1 owns fencing, command departure, and the single final outcome. |
| E12 | Claim identity and replay | Reject a mismatched copy number, session, holder, source, or generation. Lost first authorization launches zero commands; repeated consumption grants no permission. |
| E13 | Edited source or lost result | Keep fallback behind settlement. F1/G reject stale source and late fenced results before row or cache writes. |
| E14 | Compiler, shell, disk, or setup failure | E consumes a refusal without an SSH probe. F2 proves the mount, compiler, shell, and independent guard behavior. |
| E15 | Configuration and evidence refresh | Changed enabled state, spend, hold, or readiness invalidates affected evidence without permanently removing the candidate. |
| E16 | Queue responsiveness | Block evidence and claim probes independently. View, Release, and eligible local admission still answer within one second. |
| E17 | Existing routes and compatibility | Existing targets preserve configured order, capability rules, retry policy, local grant reuse, and old record decoding. |
| E18 | Failed boot and retry deadline | A failed boot produces a terminal `ExecutionReport`. Preserve the assignment's live retry deadline. Release capacity and permit fallback only after the barrier settles. |
| E19 | Queue read snapshots | Set each new field shown by `View`, `Mine`, `Explain`, `BuildHistory`, or `PlacementStatus` on the loop thread. Verify snapshot dirty marking. |

Phase B already owns exact dirty-source capture, rescue, retained target paths, and missing-basis transfer tests.
F2 owns copy acquisition, full-pool behavior, inherited locks, oversized copies, and actual command limits.
G owns linked fallback counting, historical records, host-specific totals, stop intervals, and delayed bills.
E adds boundary fixtures for those outcomes rather than recreating their implementations.

Run the changed target, eligibility, admission, hold, and Hana test modules before the checkout's targeted tests.
Run basedpyright on each changed Python file with zero errors, warnings, or notes.
Run the unchanged Mac and verify routing suites after integration.
Run no real AWS proof before the activation approvals.

## 9. Confirmed queue-owner answers

The queue owner confirmed all eight answers below.
These answers define the Hana extension to the live contract.
They are not unsettled details of queue phases 44 or 44c.

| Question | Confirmed answer |
| --- | --- |
| Does `provider=ssh` describe the target family or require the SSH command transport? | Keep it as the configured remote-target family. Select signed Hana control through `ExecutionTransport`, without changing wire literals. |
| Can the stopped box receive an assignment before a verified boot exists? | Yes, for a claim and demand start. Require verified boot evidence before adoption and queue permission consumption. |
| Which generation belongs in claim evidence? | Use the verified deployment/start/boot generation. Keep spending permit renewals separate. |
| Can a dead local holder settle a Hana attempt by itself? | No. Extend the existing pending-operation barrier with Hana lifecycle and command evidence before releasing capacity or allowing fallback. |
| How should a lifecycle wait produce notices? | Use `BusyProviderDiagnostic` for contention and `ProviderRetryNotice` through the existing wait reply for startup, holds, and recovery. |
| Can the queue evidence worker call `HanaController.request()`? | No. Evidence stays read-only; the assigned adapter commits demand through the controller. |
| Does the generic queue Linux slot equal a new cache pool? | No. It identifies the existing assignment scope. F2 uses the approved `aws-hana` maker pool without another allocator. |
| Does adding the target require another queue state conversion? | Not with these types and existing stored shapes. Require an explicit conversion plan if implementation changes that conclusion. |

The resources owner also confirms transport configuration and the remote waiter classification.
The verify owner confirms the unchanged `verify.sh` boundary and the F1 command handoff.
No question requires waiting for another queue phase.

## 10. Code estimate

The requested post-44 estimate now starts from the live `16a9610e7` placement contract.
Estimate 12–16 engineer hours for E code and verification.
This estimate excludes F1, G, F2, the owner review queue, deployment, and the paid trial.

| Work | Hours |
| --- | ---: |
| Typed transport selection, inactive configuration, and capabilities | 2–3 |
| Lifecycle and spending projections; assignment-bound claim adapter | 3–4 |
| Admission evidence, notices, and hold integration | 3–4 |
| Private tests, regression checks, type checks, and documentation | 4–5 |

The main integration risk is pending-start settlement, not unavailable queue code.
A required stored-record conversion would need a separate estimate and deployment plan.

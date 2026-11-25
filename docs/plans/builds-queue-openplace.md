# Open places, approved moves, no bumps (items 407 and 408)

> Plan only. No code before bob, then nate, say yes. Queue owns all of the change.

**The model in one paragraph.** For each target that is on, and each repository, the queue reads the open places one time in each pass. For each open place, it takes the first run in line that can use it. A run's own facts (pin, package, session, repository) are filters on that one reading. No machine answer is kept for a single run. A target that is off offers no place, and a waiting row shows no note for it. One approved move can put one exact run at the front, one time; there are no bumps.

## 1. The pass in Nate's model

1. Read the target configuration once for the pass. Include only targets that are on.
2. Read capacity and occupancy once for each enabled target and repository that waiting work can use. Count current claims and assignments.
3. Visit each open place. Scan the current queue from the first row and select the first run that can use the place.
4. Apply the run's repository, command, package, target, and session filters to the same machine reading. Apply the natedev rule in section 3.
5. Assign the place and debit its capacity in this pass. Skip the assigned request when you scan for another place.
6. Check the claim when the run starts. If the claim fails, return the run to the queue under the current retry rule.

Keep the machine reading by target and repository. A waiting run stores its own facts. A place with unknown capacity cannot supply a grant. If two places share capacity, a grant reduces both counts.

**Where the pass gets its inputs**

- `config/build_targets.conf:1-23` names the registered targets and their settings. The target status checks `enabled.<target>` and the provider switch. Native Mac uses `offload`. The guest placement check reads `enabled.mac-linux`. Hana AWS reads its own `enabled` setting. See `scripts/build_targets/target_config.py:317-347`, `scripts/mac_test/mac_test.py:612-648`, `scripts/build_queue/admission_service.py:254-268`, and `scripts/aws_target/hana_placement_adapter.py:35-54`.
- Slot capacity can come from `slots.<repository>` or the target's `slots` default. Native Mac claims read `slots.<machine>.<repository>.max` or the machine default in `config/build_folders.conf`. Mac and guest claims share a repository claim limit. The queue offers one guest place today. See `scripts/build_targets/target_config.py:167-184`, `scripts/lib/build_slots.py:100-103`, `scripts/mac_test/mac_test.py:917-940,958-987`, and `scripts/build_targets/build_targets.py:529-559`.
- A run limit can reduce the places available to new runs. On natedev, `max_runs.<machine>.<repository>` sets this limit for gate and Verify runs. The queue counts distinct runs from reservations and Verify slots. An existing run can keep using its place, and the configured exempt unit can start above the limit. See `config/build_admission.conf:26-36`, `scripts/build_queue/admission_records.py:202-219,1912-1916`, and `scripts/lint/memory_admit.py:2161-2169,2236-2251`.

**Tonight's example**

1. The AWS target is off. Native Mac, natedev, and mac-linux are on.
2. The pass finds no open `natedev/claude_commands` place and one open `mac-linux/claude_commands` place.
3. The `aws` unit's step is first in line. Its pin to mac-linux matches the open place, so it gets the assignment.
4. The `phases` step can use any eligible target. It remains in line for the next place it can use.

Here `aws` names the unit in the queue, and `mac-linux` names its target. The example uses the reported capacity and pins. Live readings were not checked.

## 2. Run facts and machine facts

Read each enabled target once for the pass. Key the reading by target, host and effective settings. Include capacity and occupancy for its repository places. Use one reading for places that share a physical resource. Read different effective settings separately when they select different hosts or provider policies.

For each open place, scan the ranked queue from the first row. Use the queue's current priority and order at `scripts/build_queue/admission_service.py:2631-2704`. Match the first run that can use the place. Debit its capacity and all overlapping capacity before matching again. Use the existing local memory walk for the natedev memory decision. Keep read failures, stale readings and occupied places distinct. Require current availability for each grant.

Here “local” means data available to the queue service. It includes submitted requests, provider configuration, the local process table, and saved records. Run filters use these inputs or select fields from the shared reading. Each filter requires no separate machine probe.

| Fact | Code | Type | Check in the pass |
| --- | --- | --- | --- |
| `HostEvidenceKey.destination`: provider, target and host | `scripts/build_queue/placement.py:260,431-442`; `scripts/build_queue/admission_service.py:4500-4509` | Machine | Read the target registry and effective connection settings. Use the configured provider, target and host to locate the reading. |
| `HostEvidenceKey.destination`: repository scope and slot | `scripts/build_targets/build_targets.py:493-526,529-559`; `scripts/mac_test/mac_test.py:910-940,958-987` | Run filter and machine places | Resolve the run's repository scope from its local source identity. Compare it with the open repository or whole-machine places. Count overlapping claims against the same capacity. |
| `HostEvidenceKey.session`: submitting session | `scripts/mac_test/mac_test.py:683-688,2893-2899`; `scripts/build_targets/build_targets.py:1626-1653`; `scripts/aws_target/hana_placement_adapter.py:456,488-489,618-622` | Run filter | Compare the session with the block or hold owner in the shared records. Keep unreadable block or claim state distinct from an absent block or claim. |
| `HostEvidenceKey.command`: argument tuple | `scripts/build_targets/build_targets.py:465-491,539-546`; `scripts/aws_target/hana_placement_adapter.py:67-91,538-564,586-595,626-631` | Run filter | Classify the submitted arguments against provider policy. Require the exact Hana GPU selection. Keep the command digest for GPU claim adoption as an assignment fact. |
| `HostEvidenceKey.configuration_generation`: settings digest | `scripts/build_queue/admission_service.py:86-147,4500-4509,5490-5499,5705-5719` | Machine configuration and run retry filter | Compute the digest once per target and effective settings. Compare it with the digest of a saved settings refusal. Save it with a new assignment. |
| `HostEvidenceKey.repository`: source repository | `scripts/mac_test/eligibility.py:299-308,467-479,512-524`; `scripts/build_targets/build_targets.py:425,493-515,549-557`; `scripts/aws_target/hana_placement_adapter.py:369-380,586-589` | Run filter | Resolve the repository key and name from the local checkout. Match the configured repository, claim scope and mirror. Preserve the local source identity of a copy. |
| `HostEvidenceKey.package`: package or workspace | `scripts/mac_test/eligibility.py:429-461`; `scripts/aws_target/hana_placement_adapter.py:538-554` | Run filter | Test a named package against `linux_only` or the GPU allowlist. With no package, apply the workspace rule to the local manifests. Keep the compile-only exception. |
| `HostEvidenceKey.step`: recorded step | `scripts/build_queue/admission_service.py:4896-4899,4933-4934`; `scripts/mac_test/eligibility.py:488-499`; `scripts/aws_target/hana_placement_adapter.py:88-91` | Run filter | Use the step to select the scaler mode or validate the Hana judgment. Map a legacy step to its command kind when the client sends no kind. |
| `HostEvidenceKey.settings`: configured provider or explicit file | `scripts/build_queue/placement.py:269-288`; `scripts/build_targets/build_targets.py:427-439`; `scripts/build_queue/admission_service.py:107-120,4494-4509` | Run filter and machine configuration | Resolve the submitted settings choice against the registry. An explicit file applies to its pinned target. Match the run with the reading for those effective settings. |
| `HostEvidenceKey.local_hold_owner`: local host, PID and start time | `scripts/build_queue/admission_service.py:4868-4886`; `scripts/build_targets/build_targets.py:1605-1615,1626-1653` | Run filter | Verify the caller identity in the local process table. Check its lineage against the shared hold's keep list. Apply only confirmed exemptions. |
| `HostEvidenceKey.command_kind`: step kind or Mac run kind | `scripts/build_queue/placement.py:291-300`; `scripts/build_queue/admission_service.py:4933-4949`; `scripts/build_targets/build_targets.py:406-408,632-652`; `scripts/mac_test/eligibility.py:467-490` | Run filter | Check the configured kinds. Apply both component policies where a pinned Linux run requires `check` and `test`. Preserve the Mac run exemption from repository step policy. |
| Target selection and fixed exclusion | `scripts/build_targets/build_targets.py:385-399,422-442,505-515`; `scripts/build_queue/admission_service.py:5506-5523,5911-5935` | Run filter | Apply the pin, Mac-only choice, local requirement, write-tree rule and repository exclusion. Keep the reason for a pinned refusal. An off target offers no place. |
| Enabled targets and transport | `scripts/build_targets/target_config.py:317-347`; `scripts/build_queue/admission_service.py:4903-4921`; `scripts/mac_test/eligibility.py:241-253,502-512`; `scripts/aws_target/hana_placement_adapter.py:35-54,513-535` | Machine configuration | Resolve enabled targets, transport, provider switches and deployment grants from the pass configuration. Keep an invalid target isolated from the other targets. |
| Holds, blocks and occupancy | `scripts/build_queue/admission_service.py:4633-4650,4868-4886,4970-5039,5973-5980,6043-6097`; `scripts/mac_test/mac_test.py:813-847,2893-2936` | Machine | Read active holds, blocks, claims, local reservations and current assignments once for the relevant resources. Use the same capacity for current starts and new grants. |
| Claim scope, owner and copy give-way | `scripts/mac_test/mac_test.py:830-847,910-914,2919-2934`; `scripts/build_queue/admission_service.py:5541-5555,6055-6064`; `scripts/aws_target/hana_placement_adapter.py:465-490,618-622` | Run filter | Match scopes, repository and exact claim identities. Compare a retry with its saved assignment. Treat allowed takeover as capacity for the matching run only. Apply copy give-way where the provider permits it. |
| Speculative copy selection | `scripts/build_queue/admission_service.py:6020-6026,6055-6064` | Run filter | Use the parent and copy order from the submitted runs. Preserve the current rule that selects the newest eligible copy for a destination. Count its parent charge. |
| Mirror timer key and version-decline key | `scripts/mac_test/mac_test.py:2881-2890,2900-2904,2941-2942`; `scripts/mac_test/eligibility.py:350-360,538-540` | Run filter | Derive the repository name from the local repository identity or the Mac mirror scope. Use that name to select its record for the target. |
| Mirror timer state and version decline | `scripts/mac_test/eligibility.py:350-360,538-540`; `scripts/mac_test/offload.py:1733-1736` | Machine, by repository | Read the timer and decline record once per target and repository in the pass. Compare the deadline against the pass time. |
| Mac disk floor and mirror setup | `scripts/build_queue/admission_service.py:4768-4789,4834-4843`; `scripts/mac_test/offload.py:2202-2249,2339-2348`; `scripts/build_targets/build_targets.py:835-852` | Machine | Read free space and requested mirror setup status in one host probe. Apply the configured floor and share the sweep state. Select setup status by mirror. |
| Battery | `scripts/buildlog/machine_facts.py:36,189-194,216-242`; `scripts/build_queue/limits_view.py:874-899`; `scripts/mac_test/test_offload.py:1477-1484` | Machine | Use battery as a displayed machine fact. The checked placement path has no battery exclusion. Keep the test that permits Mac tests on battery. |
| Reachability, failed probe and backoff | `scripts/build_queue/admission_service.py:4703-4713,4791-4815,5045-5060`; `scripts/build_targets/build_targets.py:879-910,1002-1016`; `scripts/mac_test/eligibility.py:315-347,527-535` | Machine | Share the latest reachability, failure and backoff state. Keep recovery bounded per target. A failed guest probe supplies no guest place. |
| Hana lifecycle, image, deployment and spending | `scripts/aws_target/hana_placement_adapter.py:406-454,460-506,513-535,599-617`; `scripts/aws_target/hana_spend.py:198` | Machine | Read the controller and claim records once for the box. Separate busy, starting, unavailable and unprovisioned. Include the image check and start spending preview. |
| Hana memory need and exact claim | `scripts/build_queue/admission_service.py:4596-4619,6248-6277`; `scripts/aws_target/hana_placement_adapter.py:465-482,618-631` | Run filter | Compare peak bytes with the configured step memory limit. Check the run's exact claim when it requests permission. Keep host boot identity and GPU command identity separate. |
| Scaler mode, availability and spending | `scripts/build_targets/gate_targets.py:31-51`; `scripts/build_queue/admission_service.py:169-174,3829-3832,4887-4899,5535-5537,5723-5755` | Machine, with run mode filter | Read availability for the configured modes and apply the shared capacity and spending limits. Select the mode from the run's recorded step and command. |
| Retry deadline, report time and source identity | `scripts/build_queue/admission_service.py:5472-5500,5529-5534,5541-5555,5705-5719`; `scripts/build_queue/placement.py:332-357` | Run filter | Use the saved attempt and the current configuration digest. Where required, compare the shared observation time with the report time. Bind the new assignment to the current source identity. |
| Local token, Cargo folder, memory and run limit | `scripts/build_queue/admission_service.py:3624-3643,6010-6049`; `scripts/lint/memory_admit.py:2161-2169,2223-2329` | Run filter and machine capacity | Read memory, folder occupancy and run counts from the local pass. Check the run's token state, folder, repository and peak need. Keep the existing local head rule. |

The Mac already batches SSH by connection and settings in `scripts/build_queue/admission_service.py:1497-1523,4768-4789`. It still calls `free_now` and saves a result for each policy key at `scripts/build_queue/admission_service.py:4817-4844,4980-4988`. Split `free_now` into a shared read and a run check for placement. Keep the command and provider's final claim checks.

Live machine readings and production configuration values are not checked.

## 3. natedev and memory

Today the service takes a local reading, runs the placement pass, and then runs local memory admission. The memory check uses available RAM minus the safety reserve, the running runs' keep-aside, and the swap-in allowance. The walk updates the room after each grant and accounts for the claims it releases. See `scripts/build_queue/admission_service.py:3624-3643` and `scripts/lint/memory_admit.py:659-681,2309-2316`.

When the first eligible local step cannot fit, it holds later local steps behind it. A full repository run limit holds that repository's new runs and lets other repositories proceed. The first eligible local step can start alone above room if no other run or slot holds memory and the other start checks pass. An existing memory claim can cover a continuation's need. See `scripts/lint/memory_admit.py:2187-2191,2223-2251,2259-2299,2301-2329`.

The current walk uses strict queue order for memory. The old head window, timed backfill, and `strict_room` budget rule were removed in phase 10z. The current `head_policy` calls `admission_walk`, which applies `HeldBehindHead` to later local rows. See `docs/plans/builds-queue.md:111-127` and `scripts/lint/memory_admit.py:2115-2140,2172-2179,2276-2287`.

**In nate's model:** natedev is a target like the others, and its open place also needs memory; when the first run in line that can use it does not fit, natedev starts nothing else until it fits.

**Rule: today's rule stays.** Find the first run in queue order that can use an open natedev place. If its memory need does not fit, start no other natedev run in that pass. Keep the start-alone rule and the continuation claims. Remote places continue to match. nate set strict order in his review of phase 10z, so this plan asks no question here.

## 4. What goes away

Remove these paths from placement. Use the same target reading for every run that can use the place. Produce wait reasons from that pass. Use existing reply and feed fields to display the results only.

| Remove | Code | Tests |
| --- | --- | --- |
| `_placement_hold`, `_held_for_older`, `PlacementHold`, `placement_holds` and `placement_hold_answers`. Remove the timers, saved hold answers and the hold-triggered expedite call. | `scripts/build_queue/admission_service.py:1583-1589,2189-2192,5995-6122,6124-6179`. List 394: `scripts/build_queue/admission_service.py:6032-6035,6087-6092,6120-6125,6152-6220`. | Replace the reader-expedite, one-probe deadline and no-reader tests at `scripts/build_queue/test_placement.py:4569,5324,5333`. Carry the queue-exit and ineligible-head cases at `:5353,5363` to the new pass. |
| `_newer_remote_free_readings` and `shared_free_at`. Remove comparisons between different runs' busy and free timestamps. | List 394 only: `scripts/build_queue/admission_service.py:5995-6027,6040,6087-6099,6120-6125`. | Replace the timestamp and exact-destination cases in List 394 `scripts/build_queue/test_placement.py:5390-5456`. Check first eligible rank, scope, blocks and disk from one reading. Preserve the SSH and Hana block cases at `:4482,4498` without per-run readings. |
| The retained `placement_eligibility` and `local_hold_holders` maps. Use pass-local matches and the published wait rows. | `scripts/build_queue/admission_service.py:2174-2175,3664,4077-4081,5137,5559-5564,5683-5685,5797-5971,5982-6099`. | Keep the pinned wait and refusal behavior in `scripts/build_queue/test_placement.py:1650,1686`. Keep holder lineage in `scripts/build_queue/test_admission_service.py:2598`. |
| `placement_host_keys`, `PlacementEvidenceKeys`, the per-run `HostEvidenceKey`, and per-run `host_evidence()`. Remove the run request and observation maps and their prune path. | `scripts/build_queue/placement.py:431-442,490-519`; `scripts/build_queue/admission_service.py:204,2176-2188,4500-4509,4535-4699,5148-5155,5635-5650,5960-5963`. | Replace the per-kind key contract at `scripts/build_queue/test_placement.py:4929`. Preserve session isolation at `:5139` and the Hana memory split at `scripts/build_queue/test_hana_placement.py:787`. Retest settings age and request cleanup at `scripts/build_queue/test_admission_service.py:1254,1717,1815`. |
| The per-policy worker path. Remove `HostEvidenceReader`, key subscriptions, and the loop that probes each due key. Remove the key-by-key Mac projection even when it shares an SSH command. | `scripts/build_queue/admission_service.py:1315-1356,1360-1381,1445-1569,1593-1612,4672-4689,4750-4789`. | Use the shared probe count and freshness checks at `scripts/build_queue/test_admission_service.py:2070,2074,2091,2319,2389`. Keep the socket responsiveness check at `scripts/build_queue/test_placement.py:5176`. |

The replacement worker publishes one reading per target and effective settings outside the socket reply path. Use exact dispatch evidence only for its attempt. Replace the coupling through `settlement_host_keys` at `scripts/build_queue/admission_service.py:5341-5345,5373-5379,6603-6622`. Keep claim adoption, release proof, remote-start holds, retry and assignment ownership. A remote-start hold follows a chosen assignment. The removed placement hold reserved an unassigned place for another probe.

## 5. What stays

- Keep the assignment and exact claim, the one-use launch permission, the completion report, and the retry decision. A place reading finds a candidate. The claim still controls authorization. Keep the run's place when the retry rule returns it to the queue. See `scripts/build_queue/admission_records.py:114-131` and `scripts/build_queue/admission_service.py:6180-6193,6251-6291,6326-6351,6624-6655`.
- Use the same queue order for each place. Start from the user's priority list, then use arrival order. Keep the run's place for its steps and respect saved queue moves. See `scripts/build_queue/admission_service.py:2624-2667,2698-2704` and `scripts/lint/memory_admit.py:1493-1498`.
- Keep the Builds page rows. A waiting row shows its queue position. The head row says “first in line.” Show an applicable wait reason from the pass, such as a full pool or a memory wait. Omit the name and note of an off target from the row's placement status. A starting or running row shows its assigned machine and execution status. Keep the step labels, timing, and memory fields. The feed already builds waiting rows from the published queue and projects running rows separately. See `scripts/build_queue/admission_service.py:3958-3979,4094-4105` and `scripts/build_queue/memory_feed.py:3707-3729,3752-3789`.
## 6. Parts, workers and times

Times start at nate's yes on this plan. All times are estimates, not proved. One full test run covers the parts that are ready together.

| Part | Worker hours | Files | Waits for | End (tested code) |
| --- | --- | --- | --- | --- |
| 1a. One reading per target: split `free_now` and the Hana adapter into a machine read and a run check. | 6 | `mac_test.py`, `eligibility.py`, `offload.py`, `build_targets.py`, `hana_placement_adapter.py`, `hana_placement.py`, their tests | nate's yes | with 1b |
| 1b. The open-place pass, with the removal in section 4 and the item 389 port. Fixes tonight's fault. | 8 | `admission_service.py`, `placement.py`, `admission_records.py`, queue and verify tests in section 8 | nate's yes; uses 1a's read at the end | yes + 10.5 h (review 1 h, one test run 1.5 h); merges first and alone |
| 2a. Bump removal outside the queue service: `/build:bump`, `/showrunner:bump`, the bump gate, checkpoint `--bump`, build log records, docs. | 8 | section 10D "Page, logs, and production workflow" and doc rows | nate's yes | yes + 10 h; tested with part 3 |
| 2b. Bump removal in the queue: priority list, `bumps`, person-action bump records, the page bump marks, state conversion. The natemerge bump goes in this step, when approved moves turn on. | 9 | section 10D "Controls, state, and order" and page rows | part 1 on main (same files) | part 1 + 11 h |
| 3. The approved move and the period grant. | 10 | section 10E queue and page rows, new `commands/build/move.md`, `test_approved_move.py` | 2b (same files); runs with 2b on one tree | part 1 + 12 h; one test run with 2a and 2b |

**Share no file:** 1a, 1b and 2a. They run at the same time, one worker each. 2b and 3 use the queue files of part 1, so they start when part 1 is on main.

## 7. Risks

1. **A reading is old when the pass assigns a place.** The claim check at start refuses, and the run goes back to its place in line. Test: an old free reading, a refused claim, the run keeps its rank.
2. **Two places share one capacity** (native Mac and the guest share a repository claim limit). An assignment debits both. Test: one assignment on the guest removes the matching Mac place in the same pass.
3. **A session block or hold owner becomes a run filter.** Another session must stay blocked. Test: owner, other session and unknown owner on one shared reading (the item 389 port).
4. **The mirror timer is per repository name.** Test: two checkout names, one active timer; only the run with the idle name takes the place.
5. **A run pinned to a target that is off.** No place is open for it, so it waits and blocks no other run. Its row gives its queue place and one reason from its own pin: "waits: pinned to <target>, which is off". No other row shows a note for an off target. Test: the pinned run waits with that reason; the next run takes an open place on another target in the same pass.

## 8. Files taken

Queue owns the implementation and fixture changes, including files under `scripts/direct/`. The file list uses the current evidence references. Keep the wire shape and saved assignment format where they can carry the new pass results.

| Area | Files taken |
| --- | --- |
| Queue decision, types and local memory integration | `scripts/build_queue/admission_service.py`, `scripts/build_queue/placement.py`, `scripts/build_queue/admission_records.py`, `scripts/lint/memory_admit.py` |
| Queue display and plan | `scripts/build_queue/memory_feed.py`, `scripts/build_queue/test_memory_feed.py`, `docs/plans/builds-queue-openplace.md`, `docs/plans/builds-queue-openplace-facts.md` |
| Configuration, policy, Mac and SSH reads | `scripts/build_targets/build_targets.py`, `scripts/mac_test/eligibility.py`, `scripts/mac_test/mac_test.py`, `scripts/mac_test/offload.py` |
| Hana adapter and its interface | `scripts/aws_target/hana_placement.py`, `scripts/aws_target/hana_placement_adapter.py`, `scripts/aws_target/test_hana_placement.py` |
| Queue tests and fixtures | `scripts/build_queue/admission_test_support.py`, `scripts/build_queue/test_placement.py`, `scripts/build_queue/test_admission_service.py`, `scripts/build_queue/test_admission_client.py`, `scripts/build_queue/test_hana_placement.py`, `scripts/lint/test_memory_admit.py` |
| Mac, copies, targets and gate tests | `scripts/mac_test/offload_test_support.py`, `scripts/mac_test/test_copy_watch.py`, `scripts/mac_test/test_hana_offload.py`, `scripts/mac_test/test_mac_return.py`, `scripts/mac_test/test_mac_test.py`, `scripts/mac_test/test_offload.py`, `scripts/copies/test_slot_order.py`, `scripts/build_targets/test_build_targets.py`, `scripts/production/test_gate_default_placement.py` |
| Verify tests | `scripts/direct/test_verify_mac_disk_leave.py`, `scripts/direct/test_verify_mac_release_events.py`, `scripts/direct/test_verify_ssh_targets_results.py`, `scripts/direct/test_verify_token_mac_step.py`, `scripts/direct/test_verify_token_reservation.py` |
| Verify fixtures | `scripts/direct/verify_hold_wait_test_support.py`, `scripts/direct/verify_mac_offload_test_support.py`, `scripts/direct/verify_step_routes_test_support.py`, `scripts/direct/verify_token_wait_test_support.py`, `scripts/direct/verify_tree_cancel_test_support.py` |
| As-built docs | `docs/as-built/build-memory-admission.md`, `docs/as-built/build-targets.md`, `docs/as-built/mac-test-offload.md`, `docs/as-built/builds-aws-target.md` |

AWS keeps `scripts/aws_target/cloud_gate.py`. The queue can call the existing mode-aware `availability` at `scripts/aws_target/cloud_gate.py:173`. It can keep the existing assignment entry at `:2139`. The ownership table lists the Hana files that need changes. Other indirect fixture dependencies are not checked.

## 9. Verify item 389 (kept Mac cleanup claim)

Read `738567f4e2` against its second parent. The change adds `session` with default `NoOwningSession` to `RetainedCleanupClaim` at `scripts/build_queue/placement.py:413-416`. It records that session at `scripts/build_queue/admission_service.py:5026-5030`. It changes the matching at `:5538-5562`.

1. Keep the cleanup claim's scope, identity, reason and owning session in the machine reading. Keep other sessions and unknown owners blocked on overlapping scope.
2. Apply the same-session Mac exception as a run filter. Match only a runner that can take over the retained cleanup. Preserve provider cleanup proof and the current idle rule.
3. Keep the report freshness check. An active claim from a refused attempt still blocks. For the same-session cleanup claim, require a shared observation strictly after its refusal report. Refresh the target once when the observation is too old.
4. Remove the old branch in `_candidate_eligibility` and the lookup of a per-run observation. The match uses the same claim list and observation time for all runs.

Port `test_only_the_owning_session_can_place_for_kept_mac_cleanup` at `738567f4e2:scripts/build_queue/test_placement.py:5139`. Check the owner, a different session, an unknown owner and stale evidence against one shared reading.

# Part B: approved moves, no bumps (item 408)

## 10A. The approved move

1. Select one live waiting run. Resolve its queue row to the saved run identity and confirm the identity when the approval arrives.
2. Record the exact run, the approver, the approval words, and the approval time. Record the requester's identity and a unique approval ID.
3. Save the move before the queue acknowledges it. Apply the move once and publish the updated queue.
4. Put the run at the front of the shared order before the pass selects the first run for each open place.
5. End the move when the run starts, cancels, or exits. Use the same end rule after a restart.

Use the existing run PID and start time. A row number, unit name, folder, or session can help find the run. Only the exact saved identity receives the move. A retry before launch keeps the move for that run. A completed move gives no rank to a later step, a replacement run, or a sibling run. If a second run receives a move, it goes in front at the time the queue accepts it.

Keep one source of active moves in `admission/state.json`. Add named move and approval records in `admission_records.py`. Record pending, spent, and ended states explicitly. Persist the claim that starts a run and the end of its move together. Keep a move active while its remote assignment waits for a start. Use the confirmed guest start when the assignment requires proof.

Record the original approval and the outcome in `admission/person_actions.jsonl`. Use one durable event ID per event. Save pending audit events with the state change and write each event once. On restart, finish the audit write from saved state. Reply to a repeated request with the saved result.

**Current storage and hooks.** The service saves queue state by atomic replace. It already saves run places and tracks grant and exit. The current journal stores user words and the actor. The pass imports journal entries and saves the offset. See `scripts/build_queue/execution_identity.py:22-26`, `scripts/build_queue/admission_service.py:874-893,2455-2498,2747-2767,2977-2999,6256-6291,6787,6885,7438-7480`, and `scripts/build_queue/person_actions.py:18-27,41-48,68-105,137-144`.

## 10B. Approvals with a count or a period

nate's words decide each approval. Save the agent by staff ID, the approver, the words, and the times.

1. **An approval with a count** allows that number of moves.
2. **An approval for a period** allows each move of that agent until the end time, unless the words name a count. The start is inclusive and the end is exclusive.
3. **Each use is one exact waiting run.** One use is debited in the same state change that applies the move. Expiry or revocation closes the unused part. A move the queue already accepted ends under section 10A.

## 10C. Who can approve

1. nate approves a move, or grants move authority to a named producer or showrunner by staff ID, for named units, from a start to an end time.
2. The queue saves the approver's staff ID and the approval words with each move, and the grant ID for a delegated move. It refuses a move with no words, or a delegated move with no grant in force.
3. The check is as strong as today's `/build:bump` check (actor plus the user's words). Stronger proof of who typed the words (an operator login on the Builds page for each approval) is not in this plan. See `commands/build/bump.md:5-24`, `scripts/build_queue/admission_records.py:804-805,874-879,912-914`, `scripts/production/staff.py:39-42,424-443`, `scripts/review_links/serve.py:890-916`.

## 10D. Complete removal

Bob removed six unit bumps at 21:53 PST. Nate's decision (22:12 PST, through bob): "okay keep the one bump until the approved change kicks in". The natemerge bump stays until approved moves are live. No session sets a new bump. Part 2b removes the natemerge bump in the same step that turns on approved moves (part 3 on the same tree). Live state was not checked. The `max_runs.exempt_unit` setting controls a run limit in `config/build_admission.conf:26-36`. It is a separate rule.

### Controls, state, and order

| Remove or replace | Current source |
| --- | --- |
| Remove the unit bump command and its standing permission text. Remove the CLI writer and retain any shared row selection helper that the new command uses. | `commands/build/bump.md:5-24`, `scripts/build_queue/build_queue.py:22-83,86-176` |
| Remove `SetBump`, `ClearBump`, `Bump`, `BumpNotApplied`, `AppliedBump`, and `BumpProgress`. Replace the bump journal reader and writer with the move audit writer. | `scripts/build_queue/person_actions.py:18-144` |
| Remove `bumps` and the priority list from state. Remove roster changes, priority history settlement, and the pass that applies bumps. Remove the guest row check that reads `self.bumps.moves`. | `scripts/build_queue/admission_service.py:874-893,1756-1808,2219-2220,2277-2289,2322-2328,2450-2459,3588,4010,7314-7480,7858-7985` |
| Remove `QueuePriority`, the unit and legacy run entries, and their rank and transition records. Publish the one order. Apply it to local, remote, cloud, and kept-place rows where they read the current order. Preserve their existing capacity and resource checks. | `scripts/build_queue/admission_records.py:760-914,1543-1577,1628-1664`, `scripts/lint/memory_admit.py:320-323,346-508,1493-1498,2616-2636,2926-2940`, `scripts/build_queue/admission_service.py:2624-2708,3188-3189,3322-3328,3746-3782` |
| Remove `priority-set` and `priority-clear` from the socket, client, CLI, and service dispatch. Retire `queue_priority.json` import and `export-priority`. Drop old priority fields at the state and snapshot conversion boundary. | `scripts/build_queue/admission_client.py:1640-1647,2396-2400,2451-2457`, `scripts/build_queue/admission_protocol.py:60-72`, `scripts/build_queue/admission_wire.py:335-351,768,798-806,847-849`, `scripts/build_queue/admission_service.py:6927-6946,7113-7120,8033-8049,8175-8235`, `scripts/lint/memory_admit.py:346-379` |

Convert private copies of old state before deployment. Keep live reservations, run places, attempt records, and history unrelated to bumps. At cutover, stop old writers and archive the old journal. Ignore its queued and active entries on restart. Discard old priority entries during conversion. Keep the drag entries and queue pins of nate's drag. A rollback must start with all legacy bump entries inactive. Keep the archive as history and keep it outside the live replay path.

### Page, logs, and production workflow

| Remove or replace | Current source |
| --- | --- |
| Remove the unit priority heading, priority tooltip, applied mark, pending and refused notices. Keep nate's drag controls as they are. Show queue position and an approved move note on its exact waiting run. Show the approver and approval words in the detail. Remove the note when the move ends. | `scripts/build_queue/head_facts.py:8-32`, `scripts/build_queue/memory_feed.py:566-590,1328,1377-1381,3327-3356,3903-3922,3974-3978`, `scripts/build_queue/memory_live.html:1726,2025-2120,2451,3112-3262,3344-3382`, `scripts/build_queue/builds_page_real_rows.json:22,233-237,302-306,369-373`, `config/build_queue.conf:1-13` |
| Remove the `queue_priority` build log writer and the old `queue_bumps` index projection. Keep original history files. The move journal supplies the audit record. | `scripts/buildlog/record.py:1476-1495`, `scripts/buildlog/index.py:486-496,531,1164-1172` |
| Remove `/showrunner:bump` and its special checkpoint instructions. Remove `--bump`, the push-before-gate path, and the dedicated gate and result record. Leave ordinary checkpoint tests and promotion in their existing order. Finish active legacy gates before retirement. | `commands/showrunner/bump.md:5-53`, `commands/showrunner/produce.md:600,616-617`, `commands/unit/merge.md:156-165`, `scripts/production/merge_checkpoint.py:169,289-294,331,383,1508,1607-1690,1829-1945,2746-2809`, `scripts/production/bump_gate.py:1-78`, `scripts/production/checkpoint_gate.py:416-516`, `config/build_folders.conf:61` |
| Refresh the generated skills so the removed commands have no entry point. The sync script removes stale generated skill directories. | `scripts/claude_to_codex/sync.py:196-210,235-240` |

The search also finds version changes and stall-watch messages. Stall watch sends idle notices through session messaging. See `commands/release.md:584-588` and `scripts/production/stall_watch.py:1527-1628`.

### Tests and fixtures

- Replace the old command and ranking proofs with tests for exact identity, authorization, expiry, replay, start, and exit. Cover duplicate requests and a sibling run that keeps its arrival order. See `scripts/build_queue/test_build_queue.py:40-344`, `scripts/build_queue/test_admission_client.py:1313-1318,1848-1850`, `scripts/build_queue/test_admission_protocol.py:577,679`, `scripts/build_queue/test_admission_reload_order.py:105-171`, and `scripts/build_queue/test_admission_service.py:207-230,3801-3821,4070-4081,4286-4540,6629-6639,6952-7010,8036-8054,8553-8915,9540-9555,10337-10360,10587-10605`.
- Extend the order tests to prove the same move order reaches local and remote selection. Keep claim return, start holds, landing holds, and kept-place tests that still apply. Exercise old state with bump entries. Keep the drag and queue pin tests as they are. See `scripts/build_queue/test_queue_order.py:102-839,1007-1217,1397-1840`, `scripts/build_queue/test_placement.py:2053`, `scripts/build_queue/test_queue_view.py:715-733`, `scripts/build_queue/queue_fixture.py:1-100`, `scripts/direct/test_verify_mac_disk_leave.py:138`, and `scripts/lint/test_memory_admit.py:2679-2877,3879,6298,6734-6738`.
- Replace bumped page, JSON, and browser cases. Prove the exact waiting row shows its approval and that the note leaves with the move. Refuse legacy requests, unapproved moves, and actors with only a service account. See `scripts/build_queue/test_head_facts.py:9-30`, `scripts/build_queue/test_memory_feed.py:1042-1074,3203-3336,5432`, `scripts/build_queue/test_memory_hover.py:471-532,1548-1607,2566-2567,2914-2945`, `scripts/build_queue/memory_page_proof.mjs:842-854,1071-1213,1486,2763-2806,3547-3548,3742-3758,3840,4020`, and `scripts/review_links/test_serve.py:7230`.
- Replace dedicated bump gate and history fixtures. Use an ordinary gate for shared slot and package tests. Keep the folder permission and cleanup assertions with an active prefix. Update the Mac module list to match the retained tests. See `scripts/copies/test_slot_order.py:48,359-409`, `scripts/lib/test_build_folders.py:1907-1915,2089-2132`, `scripts/production/merge_checkpoint_test_support.py:182-245,502-686`, `scripts/production/test_bump_gate.py:1-81`, `scripts/production/test_merge_checkpoint_bump.py:1-508`, `scripts/production/test_merge_checkpoint_promotion.py:185-461`, `scripts/buildlog/test_index.py:1078,1785-1799`, `scripts/buildlog/test_record.py:705-748`, and `config/mac_gate_modules.txt:24,225`.

### Documentation and memory

Remove old operating instructions. Mark historical plan entries as superseded where they still describe an available control.

| Area | References |
| --- | --- |
| Queue operations and page | `docs/as-built/accounts-queue-panes.md:7-9`, `docs/as-built/build-memory-admission.md:44,267,436,495,531-637,655,712-736,810,1258,1394-1400,2312-2336,2420,2728-2730,2784,2867-2893`, `docs/as-built/build-targets.md:286`, `docs/user_facing_explanation.md:135-143` |
| Checkpoints and gate tools | `docs/as-built/merge-gate-and-test-speed.md:142,160,983`, `docs/as-built/repair-rounds.md:178`, `docs/as-built/showrunner-automation.md:236,256-264`, `docs/as-built/staff-accessors.md:275`, `docs/plans/build-folders.md:224`, `docs/plans/builds-natest-next.md:20`, `docs/plans/builds-natest-streamline-a.md:157,444-461,527-535,2483`, `docs/plans/builds-queue-placement.md:399`, `docs/plans/merge-gate-instances.md:311` |
| Earlier queue plans | `docs/plans/builds-natest.md:999`, `docs/plans/builds-production.md:37`, `docs/plans/builds-queue-service-design.md:59-125`, `docs/plans/builds-queue-view-brief.md:35`, `docs/plans/builds-queue.md:34-55,76-127,333-365,520,857-1032,1246,1288,1469,2121-2130,2346-2403`, `docs/plans/builds-report.md:282-295,867,1283-1286,1961`, `docs/plans/builds-resources-next.md:42-44`, `docs/plans/builds-resources.md:378,621-653,752-770,1001-1015,2631` |

Memory references under `~/.claude/projects/-home-natepiano--claude/memory/`:

- `only-user-bumps.md:3,11-26`
- `queue-commands-any-session.md:3,7`
- `natemerge-gates-unit-tests.md:3,11`
- `natemerge-runs-first-always.md:11,25`
- `mac-linux-guest-first.md:8`
- `propose-speedups-proactively.md:3,11-13`
- `new-request-goes-last.md:12`
- `merge-tests-first-during-aws-hold.md:21`
- `checkpoint-subject-carries-change-review.md:23`
- `attempts-44fg-queue-drag.md:27-31`
- `attempts-bump-refusal-forwarding.md:2-15`
- `MEMORY.md:27`, `index-attempts.md:26`, and `index-builds-gates-tests.md:100`

## 10E. Files taken

Section D names the files for removal and conversion. This list covers the same implementation scope and names the new command and test.

| Area | Files |
| --- | --- |
| Queue, state, and protocol | `scripts/build_queue/{admission_service,admission_records,admission_wire,admission_protocol,admission_client,person_actions,build_queue,head_facts,queue_fixture}.py`, `scripts/lint/memory_admit.py` |
| User controls | `commands/build/bump.md`, new `commands/build/move.md`, `commands/showrunner/{bump,produce}.md`, `commands/unit/merge.md`, `scripts/review_links/serve.py`, `config/build_queue.conf` |
| Builds page | `scripts/build_queue/{memory_feed.py,memory_live.html,builds_page_real_rows.json,memory_page_proof.mjs}` |
| Build log and checkpoint | `scripts/buildlog/{record,index}.py`, `scripts/production/{bump_gate,checkpoint_gate,merge_checkpoint,merge_checkpoint_test_support}.py`, `config/build_folders.conf` |
| Tests | `scripts/build_queue/{test_admission_client,test_admission_protocol,test_admission_reload_order,test_admission_service,test_build_queue,test_head_facts,test_memory_feed,test_memory_hover,test_placement,test_queue_order,test_queue_view}.py`, new `scripts/build_queue/test_approved_move.py`, `scripts/buildlog/{test_index,test_record}.py`, `scripts/copies/test_slot_order.py`, `scripts/direct/test_verify_mac_disk_leave.py`, `scripts/lib/test_build_folders.py`, `scripts/lint/test_memory_admit.py`, `scripts/production/{test_bump_gate,test_merge_checkpoint_bump,test_merge_checkpoint_promotion}.py`, `scripts/review_links/test_serve.py`, `config/mac_gate_modules.txt` |
| Docs | The documentation files in section D. Join this plan with `docs/plans/builds-queue-openplace.md` in the director's plan. |
| Deployment | Retire the legacy state fields and journal path in use. Refresh the generated Codex skills. The memory files above are references only. |

Use the staff reader as the source of member IDs and roles. The trusted session endpoint for delegate approvals may require files outside this list. Its implementation is not checked.

## 10F. For nate, after the plan go

1. **nate's drag on the Builds page stays as it is** (nate, 22:29 PST: "if i drag a build on the page manually it can go any place"). He can drag a row to any place. Keep its code and tests: the drag controls, neighbor pins, undo and the service move path. Do not limit it and do not remove it.
2. The approved move is the only move of an agent. No agent can use the drag; it stays nate's own control.

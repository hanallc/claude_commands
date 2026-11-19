# Gate child exit and memory accounting

Phase 391 design. The line ranges refer to `origin/main` at `673b10fa82d2664b468c2fef814d03522ee0b602`. Builds orders the implementation after phase 388.

## Current waits

The lines in this table are in `scripts/production/merge_gate.py`.

| Lines | Interval or deadline | What the gate waits for |
| --- | --- | --- |
| 366–391 | Remaining gate deadline, or no timeout | When no job runs, wait for the shared CPU token or the cancellation pipe. |
| 395–404, 1700–1709 | Zero per readiness check. The suite retries after a 50 ms sleep when jobs still run. | Check whether the CPU pool has a token without blocking. |
| 522–530 | Up to 15 s per wait | Wait for a hold release event. Recheck the holder state when the event arrives or the wait ends. |
| 594–600, 1976–1981 | 200 ms by default, up to the remaining 60 s default deadline | Retry the release lock before main promotion. |
| 710–724 | Remaining `program_stop_seconds`, 5 s in the committed config | Wait for recorded group leaders after TERM, then send KILL to their groups. |
| 725–740 | Up to `program_stop_seconds` per member | Wait for a recorded group's live members before recovery deletes files. |
| 989–998, 1592–1602, 1763–1764 | 50 ms between suite scans | Check every job with `wait4(WNOHANG)`. Check expiry for each running module. The default module limit is 1800 s. |
| 1013–1030 | `program_sample_seconds`, 1 s in the committed config, up to the remaining stop deadline | Send TERM to all affected groups. Check for leader exits until all leaders exit or the deadline expires. Send KILL and reap. |
| 1566–1569, 1657–1672, 1763–1764 | 50 ms between checks. The gate deadline comes from history and excludes hold time. | Stop the gate when the full run reaches its limit. |
| 118, 302–315, 1673–1692 | Memory sample due every 250 ms after the last sample | Read memory for admission and stop a module that exceeds the governor. The 50 ms loop checks when the sample is due. |

At lines 548–554, `communicate()` and the final `wait()` have no caller timeout. The final `wait4` at lines 989–1007 also blocks without a timer when the gate reaps a killed leader.

Other bounded waits start a command once. They cover the memory stop notice (467), type environment build (943, 600 s), and remote state check (1894, caller budget). They also cover the promotion push (2030, 25 s), style notice (2067, 30 s), and nightly Git (2557, 300 s). The nightly notices (2305, 2441), time limit notice (2696), and slot sweep notice (2761) use the configured send budget. The scope probe that line 1501 calls has a 10 s budget in `scripts/build_queue/queue_place.py:98–112`.

## Current memory reads

| Lines | Read |
| --- | --- |
| 613–638 | List `/proc`. Read `stat` for every numeric entry to get parent PID and resident pages. Build a descendant set for each root. |
| 603–610, 638–639 | Read `VmSwap` from `/proc/<pid>/status` for each PID in each root's descendant set. Add swap to resident pages. |
| 1673–1692 | Use the gate PID for total memory and each running job PID for job memory. The same process can contribute to both totals. |

`/proc/loadavg` at lines 1075–1076 reads host load. The pass at lines 725–740 reads group membership during recovery. Both reads serve separate purposes.

The AWS paired trial also calls `process_memory_mib` in `scripts/aws_target/gate_box.py:123,308–316`. Retain its existing helper for that caller. The suite scheduler will stop using that helper.

## Design

### Wait for work

1. Open a pidfd for each direct child while launch defers cancellation. Register the child and its recovery record in the same protected section.
2. Use one scheduler poll set for live pidfds. Watch the shared CPU descriptor only when its readiness can start a pending job.
3. Wait until a descriptor is ready or the nearest deadline expires. Include the gate deadline, module deadlines, and the next required memory sample.
4. Read exit status and CPU with `wait4` once per leader. Drain ready exits before checking deadlines, including when a poll returns at a deadline.
5. Remove and close each pidfd when the gate collects its exit. Close remaining descriptors on launch failure, cancellation, and cleanup.

Keep the current idle CPU and hold waits. Recompute the gate deadline after a hold. Run pending work immediately when the scheduler can start it. When a retry is ready and no job runs, start it in the next pass without a wait. A blocked token check keeps the CPU descriptor registered until the gate can take a token.

Shared mode keeps the 250 ms memory deadline while jobs run. It uses that deadline to detect memory growth and admit work. Whole-machine mode has no memory deadline. Neither mode needs a 50 ms liveness timer. Keep the current signal dispositions and cancellation deferral.

### Read owned cgroups

1. Use a distinct cgroup v2 leaf for each shared-mode test job and type check. Keep the job inside the outer admission and memory limits.
2. Create the leaf inside a delegated parent. Record its unique path, device, inode, and boot identity. Attach the direct child through `cgroup.procs` before releasing the bootstrap to execute the job.
3. Keep the leaf until the final read. Open its counters once and read them at offset zero on each sample. Read `memory.peak` again before removing the leaf.
4. Use `memory.current` plus `memory.swap.current` for the live charge. Keep the largest sampled live charge and the kernel's `memory.peak` as the job peak.
5. Compute the gate charge from simultaneous job charges and the gate's own resident and swap reading. Read the supervisor's `statm` and `status` directly.

The final `memory.peak` captures a short peak between samples. The sum of independent job peaks cannot represent concurrent gate memory. Keep the current reservation padding, fallback sizes, and history format. Record successful job peaks in the existing history. Keep the current live-charge limit for stopping a running module. The cgroup charge includes memory such as page cache and kernel allocations, so compare admission and memory stops on the paired runs.

Builds must confirm a delegated parent for each supported shared launch. The existing scope prefix skips jobs when the gate already runs in `builds.slice`. A sibling scope can move a job outside an outer memory limit. Use a delegated child of that limit in this case. Check the memory controller and counters before starting shared work. Report a refusal if the gate cannot obtain an owned leaf. Builds must approve this change to the current scope-probe fallback before implementation. A private gate fixture can supply the cgroup interface without a live systemd manager. Whole-machine mode keeps its existing scope and admission behavior.

### Keep stop and recovery order

1. Collect completed jobs first. Then handle module timeouts, the gate timeout, memory stops, and new starts in the current order.
2. Send TERM to all affected groups before reaping any leader in that stop pass. Use the pidfds and the same shared stop deadline for the grace wait.
3. Send KILL to surviving groups and finish reaping. Clear remaining writers in each owned cgroup and confirm `cgroup.events` reports `populated 0` before removing that job's files.
4. Preserve exit status, CPU, logs, the memory stop rule for zero exits, and retry eligibility. Remove the job record only after cleanup verifies its ownership state.
5. Keep 383's owner and process start checks, group records, token rules, folder records, and merge rollback. Add optional scope identity to the record before the child runs.
6. During dead-owner recovery, perform the existing group stop first. Verify the owned scope identity and stop remaining writers before returning tokens, removing folders, and undoing the merge.

Older records have no scope field. Keep their current recovery path. If the scope identity is uncertain or writers remain, retain the recovery record and folder. A closed pidfd only removes the gate's reference to the descriptor. It does not prove the cgroup is empty.

## Implementation paths for Builds

Add both new rows to `test_merge_gate_suites_resources.py`. The current fixtures also patch the sampler or construct jobs directly. These migrations need Builds' approval with the production file before coding.

| Path under `scripts/production/` | Base lines | Change |
| --- | --- | --- |
| `merge_gate.py` | 4–17, 118–132, 257–328 | Document the charge. Add launch and job ownership state. Keep the admission clock and reservation arithmetic. |
| `merge_gate.py` | 652–787, 816–880, 989–1030, 1086–1105, 1137–1165 | Add scope ownership to 383 recovery. Attach and register each child. Wait for exits during stop and read the final peak before cleanup. |
| `merge_gate.py` | 1501–1533, 1561–1573, 1590–1614, 1629–1692, 1693–1778, 1816–1820 | Prepare shared job cgroups. Replace suite samples and waits. Keep the limit and result order. Label the reported memory source. |
| `test_merge_gate_suites_resources.py` | 15–29, 61–121, 384–399, 477–505, 547–592 | Add the wake and counter rows. Retain the existing scheduler and timeout assertions with private readings. |
| `merge_gate_test_support.py` | 74–86, 383–419, 432–459, 674–698 | Supply private counter files to both fixture entry points. Keep deterministic job release and the existing scope tests. |
| `test_merge_gate_suites_admission.py` | 76–105, 420–483, 541–630, 643–711 | Adapt startup observers, peak and swap fixtures, and the sample clock. Retain the admission and reservation assertions. |
| `test_merge_gate_suites.py` | 33–78, 128–161, 299–328 | Adapt the controlled clock and fake child registration. Keep the deadline tie and whole-machine assertions. |
| `test_merge_gate.py` | 473–488 | Release the type job on the private memory reading. |
| `test_merge_gate_holds.py` | 125–155, 209–232 | Pass the launch ownership argument through existing observers. Keep the hold and restart assertions. |
| `test_merge_gate_suites_types.py` | 142–161 | Pass the launch ownership argument through the type job observer. |
| `test_merge_gate_test_times.py` | 83–105, 218–227, 256–275, 288–317 | Feed private peaks to the record fixtures. Keep exception, hold, and result assertions. |

Phase 383's local branch is `natest-gaterecover383`. Commit `5e578970a` changes token recovery near base lines 743–759. Keep its FIFO identity check and device token behavior. Preserve the shared stop order at 989–1030 and ownership publication at 1093–1101. Run its signal and recovery rows after implementation.

Phase 388's `natest-cleanenv388` owns `test_module_environment` at 1108–1134. Its callers include the job start at 1722–1725. Preserve the allow list for test jobs. Keep scope identity and descriptors in the supervisor. Integrate phase 388's fixture edits before changing the shared fixture.

Phase 355's `natest-chainmerge` places the implementation in `gate.py`. Apply these changes to the active implementation and the corresponding `gate_test_support.py` and `test_gate*.py` names after that merge. Preserve the imports that phase 355 carries across the rename.

## Test rows and CPU estimate

1. Hold a real fixture job on a pipe in whole-machine mode. Advance a controlled clock through multiple 50 ms ticks before the nearest deadline. Count zero scheduler wakes before releasing the job. Then expect one exit wake and one `wait4` result. Count the timed sleep and child checks when running the row against the base.
2. Open private cgroup counter files with distinct live, swap, and peak readings. Run an admitted job while a second job waits. Assert the live charge drives admission, the module stop reads the live charge, and the final result records the retained peak. Make the process-tree sampler raise if the suite calls it. Exercise a missing counter and a leaf that remains populated during cleanup.

Use private fixture configuration and controlled clocks. Run the rows on the base first after natemerge clears tests. Then check timeout ties, TERM order, late exits, hold restart, retry failure, and 383 recovery with the existing rows. Run basedpyright on the changed files. Repeat the gate modules 20 times with `TMPDIR=/tmp` and a 2 GiB scope. Run the complete natedev gate when authorized.

Phase 385's saved reply attributes 45.3% of its baseline trace CPU to child runners. It reports a 30.495 s to 13.201 s median change for the ledger fixture. The linked detail report was absent at its stated path. The fixture result cannot quantify the production supervisor saving.

Estimate 20 timer wakes per quiet second on the base, compared with zero in whole-machine mode and up to four sample wakes in shared mode. Exit, CPU token, and deadline events can add wakes. With 300 visible PIDs and 16 jobs, each sample replaces at least 300 process reads with at most 50 counter and supervisor reads. The counts are illustrative. Expect lower supervisor CPU if reduced reading and wake work exceed cgroup setup and cleanup cost.

Measure the supervisor's user and system CPU separately from child CPU in a paired, full local run. Match the module set, job limit, admission setup, and cache state. Report the absolute CPU change, wake counts, and verdicts. Include scope setup cost in the result. No phase 391 measurement has run.

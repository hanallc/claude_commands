# Gate child exit and memory accounting

Phase 391 waits for bb1 (phase 383) and phase 392 to land on main. Builds orders the implementation. The base line ranges refer to `origin/main` at `673b10fa82d2664b468c2fef814d03522ee0b602`.

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

Keep `process_memory_mib` for the fallback sampler. The AWS paired trial also calls it in `scripts/aws_target/gate_box.py:123,308–316`.

## Design

### Wake on exit

1. Register a pidfd for each child while launch defers cancellation.
2. Wait in one poll set for pidfds, an eligible CPU token, or the next deadline.
3. Reap ready children with `wait4` before applying module, gate, and memory limits in that order.
4. Close each pidfd on reap or cleanup.

Keep the current hold and idle CPU waits. Shared mode keeps its 250 ms memory deadline. Platforms without pidfds keep the current child wait. Keep the current signal dispositions.

### Read the module scope

Phase 392 uses the existing `systemd-run` prefix when the scope probe succeeds. It assigns each test module a named scope under `builds.slice`. It saves the path before launch and keeps it in `RunningGateJob.scope`. See `natest-testwalls392` at `6a7a83a1e`, `merge_gate.py:1625–1632,1875–1893`.

1. Read `memory.current` and `memory.swap.current` from that module's scope for the live charge.
2. Record the greater of `memory.peak` and the highest live charge. Keep the last valid peak if systemd removes the scope before the final read.
3. Sum simultaneous job charges and the supervisor's resident and swap readings when all jobs have readable scopes.
4. Use the current process-tree sampler for unscoped jobs and for the total when any running job lacks a readable scope.

The fallback covers a failed probe or join, private gates, unscoped type checks, and Mac. Missing scope accounting never refuses a job. Keep the current reservation padding and memory stop threshold. Whole-machine mode keeps its current admission behavior.

### Preserve cleanup

Phase 392 writes `cgroup.kill` and waits for `cgroup.events` to report `populated 0` or for the scope to disappear. Keep its deadline, process-group fallback, and folder retention on unresolved stops. See `merge_gate.py:1025–1151,1260–1264` at `6a7a83a1e`.

Reuse the saved scope in recovery. Preserve 383's process identity checks and token rules. Keep 392's scope and group checks before token return, folder removal, and merge rollback. See `merge_gate.py:708–776` at `6a7a83a1e`.

## Implementation paths for Builds

Add both new rows to `test_merge_gate_suites_resources.py`. Check these base ranges again after the merges. Keep existing sampler mocks for unscoped fixtures where they still apply.

| Path under `scripts/production/` | Base lines | Change |
| --- | --- | --- |
| `merge_gate.py` | 4–17, 257–328, 989–1030, 1086–1105, 1137–1165 | Add pidfd ownership and collect the last available memory peak before cleanup. |
| `merge_gate.py` | 1501–1533, 1561–1573, 1590–1614, 1629–1778, 1816–1820 | Use the 392 module scope for memory. Use the current sampler where needed. Label the memory source. |
| `test_merge_gate_suites_resources.py` | 15–29, 61–121, 384–399, 477–505, 547–592 | Add the wake and counter rows. Retain the existing scheduler and timeout assertions with private readings. |
| `merge_gate_test_support.py` | 74–86, 383–419, 432–459, 674–698 | Supply private counter files to both fixture entry points. Keep deterministic job release and the existing scope tests. |
| `test_merge_gate_suites_admission.py` | 76–105, 420–483, 541–630, 643–711 | Adapt startup observers, peak and swap fixtures, and the sample clock. Retain the admission and reservation assertions. |
| `test_merge_gate_suites.py` | 33–78, 128–161, 299–328 | Adapt the controlled clock and fake child registration. Keep the deadline tie and whole-machine assertions. |
| `test_merge_gate.py` | 473–488 | Release the type job on the private memory reading. |
| `test_merge_gate_holds.py` | 125–155, 209–232 | Check launch observers and retain hold and restart assertions. |
| `test_merge_gate_suites_types.py` | 142–161 | Check launch observers for unscoped type jobs. |
| `test_merge_gate_test_times.py` | 83–105, 218–227, 256–275, 288–317 | Feed private peaks to the record fixtures. Keep exception, hold, and result assertions. |

Keep the FIFO check from 383 (`natest-gaterecover383`, `5e578970a`). Preserve the 388 environment allow list and fixture edits from `natest-cleanenv388`. Use the `gate.py`, `gate_test_support.py`, and `test_gate*.py` names if the 355 rename has landed.

## Test rows and CPU estimate

1. Hold a fixture child before the next deadline. Check zero scheduler wakes until release, then one exit wake and one `wait4` result. Count the base's 50 ms wakes.
2. Supply private scope counters. Check the live charge, recorded peak, and memory stop. Check fallback accounting after an unscoped launch, failed join, or missing counter. Check that the result keeps its last peak if systemd removes the scope.

1. Run both rows on the base first after natemerge clears tests.
2. Check deadlines, stop order, hold restart, and 383 and 392 recovery with the existing rows.
3. Run basedpyright on changed files.
4. Repeat the gate modules 20 times with `TMPDIR=/tmp` and a 2 GiB scope.
5. Run the complete natedev gate when authorized.

Phase 385 measured median ledger fixture CPU at 30.495 s before its change and 13.201 s after. Estimate fewer supervisor wakes and memory reads for phase 391. Use matched local gate runs to measure the supervisor's CPU separately from child CPU. Record wake counts and verdicts.

# List item 141 — A test cannot delete or change live folders

This is Part 1 of phase 389. The producer and reviewer is `bob`. The plan is due on 2026-11-19 at 15:00 EST.

Cover every test in `claude_commands` on natedev, across all productions and units. Include manual tests and every descendant of a test.
Part 2 covers process visibility, outside access, and the Mac and AWS forms. The start inventory below includes the remote paths.

On 2026-11-19 at 12:46 EST, a queue sweep test used a live default and deleted 11 entries in `/tmp/claude/unit`.
On 2026-11-14, an inherited `GIT_DIR` let fixture commits reach live `~/.claude` main.
Builds reported a test run that used the inherited `TMPDIR` and wrote under `/session/queue`.
A wrong path can come from an argument, an environment variable, a config file, or a direct absolute path.
The kernel must refuse the write even when the caller chooses the wrong path.

## Live roots on natedev

Keep host files read-only or hidden in a test. Verify each named root and alias before launch.

| Root | Evidence at base `81ac379ca` |
| --- | --- |
| `/tmp/claude`, including `unit`, `delegate`, and `astra` | `TMP_ROOT`, `default_roots()`, and `run_roots()` select these roots. `stat` confirmed `/tmp/claude`. [tmp_sweep.py:134](../../scripts/lint/tmp_sweep.py#L134), [unit_runs.py:33](../../scripts/lib/unit_runs.py#L33) |
| `/tmp`, `/var/tmp`, `/tmp/claude-1000`, and `/dev/shm` | `stat` confirmed all four. The sweep lists per-user scratch under `/tmp`. The gate uses `/dev/shm/merge-gate-tests`. [build_folders.conf:10](../../config/build_folders.conf#L10), [tmp_sweep.py:410](../../scripts/lint/tmp_sweep.py#L410), [merge_gate.py:1407](../../scripts/production/merge_gate.py#L1407) |
| `/session`, `/session/queue`, `/build/session`, and `~/session` | `findmnt -T /session` reported `/dev/mapper/build[/session]`. The config selects `/session` here and `~/session` on other hosts. `~/session` was absent here. [build_folders.conf:27](../../config/build_folders.conf#L27) |
| `/build`, including `claude/scratch`, `claude/copies`, `cargo`, `paths`, `slot`, and `hana-ci` | `findmnt -T /build` reported its own writable ext4 mount. The build folder and lint configs name these folders. [build_folders.conf:13](../../config/build_folders.conf#L13), [lint.conf:197](../../config/lint.conf#L197) |
| `~/.claude`, `~/worktrees`, and `~/rust`, including `~/rust/hanadocs` | `stat` confirmed the live checkout, worktree root, and vault. `git rev-parse --git-common-dir` resolved this worktree to `~/.claude/.git`. The config names planned shell and mirror roots under `~/rust`. [build_folders.conf:24](../../config/build_folders.conf#L24), [staff.conf:13](../../config/staff.conf#L13) |
| `~/.local/state`, `~/.config`, `~/.cache`, `~/.codex`, `~/.cargo`, and `~/.rustup` | `stat` confirmed these roots. Staff and build log code select live state here. The Codex quota reader selects `~/.codex/sessions`. [staff.py:815](../../scripts/production/staff.py#L815), [store.py:46](../../scripts/buildlog/store.py#L46), [codex_pacer.py:12](../../scripts/whoami/codex_pacer.py#L12), [lint.conf:194](../../config/lint.conf#L194) |
| `~/.local/share/review-links` | Change review stores its links here. [change_review.conf:7](../../config/change_review.conf#L7) |
| Numbered Claude and Codex homes, `~/.claudeN` and `~/.codexN` | Check every configured numbered home and its aliases. The account helpers construct these paths. [accounts.py:655](../../scripts/accounts/accounts.py#L655), [accounts.py:669](../../scripts/accounts/accounts.py#L669) |
| `/etc/nixos` | The account config names the system repository here. It contains the natedev jobserver configuration. [accounts.conf:97](../../config/accounts.conf#L97), [invoke.sh:61](../../scripts/lint/invoke.sh#L61) |
| `/run`, including `/run/user/1000`, plus `/var/lib/hana-ci` | `stat` confirmed the user runtime directory. The user manager provides `XDG_RUNTIME_DIR`. Lint names the CI roots. [invoke.sh:603](../../scripts/lint/invoke.sh#L603), [lint.conf:197](../../config/lint.conf#L197) |
| Any absolute or configured path outside the leased test folder | The inherited temporary parent can come from `TMPDIR`. The mount rules also cover unlisted roots and aliases. [build_folders.py:192](../../scripts/lib/build_folders.py#L192) |

An environment override can select another data root. It cannot grant write access.
A bind mount can expose the same files at two paths. Check the mount tree, including child mounts, before starting the command.

## One file guard at test start

Use the same Bubblewrap launcher for all three guards on natedev. Bubblewrap provides the mounts, namespaces, and PID 1 reaper in one launch.
Start the guard after trusted setup and before any candidate interpreter, shell, test binary, or build script.
Use the [Part 2 launch profile](test-live-folder-guard-walls.md#natedev--one-guarded-launch) for process and outside-access controls.

1. Allocate one leased folder with the build folder maker.
2. Create the run's `TMPDIR`, `HOME`, XDG directories, logs, and writable fixtures inside it.
3. Select the source checkout and any read-only build inputs.
4. Start Bubblewrap inside the verified job scope with an empty root and the namespaces from Part 2.
5. Add approved inputs with `--ro-bind`, a fresh private `/proc`, and the devices listed below.
6. Use `--bind` for the job's leased storage.
7. Apply `--remount-ro /` and `--cap-drop ALL`. Bubblewrap sets `no_new_privs` before exec.
8. Send output through pipes to the trusted parent.
9. Release the folder only after the supervisor proves that the owner and all test descendants exited.

Reuse `test_run_folder()` and the existing build folder leases. Use an explicit parent from the supervisor, inside the new writable mount.
Keep `BUILDLOG_DIR`, the session fixture root, Python bytecode, and browser profiles inside the lease.
Do not reuse the caller's `DEVTOOLS_TEST_RUN_FOLDER` or `TMPDIR` as a writable mount.
Replace `TMPDIR`, `TMP`, and `TEMP` before interpreter startup. Clear the inherited run-folder marker.
Keep nested allocations under the lease, including when a helper selects a sibling of an existing run folder.
Pass the private parent explicitly to helpers that allocate or sweep test folders.
Keep run output and worker records in the trusted parent. Keep one new folder per invocation and one private child folder per gate module.
Keep recovery, lease ownership, and the locks in `copies.COMMAND_LOCKS` in the trusted supervisor. Coordinate lock ownership with phase 383.
Close all inherited descriptors before the candidate exec except the selected standard streams. Use pipes for output and a pipe or private device for input.
Use the phase 388 environment allow list before the first launcher exec. Use trusted absolute paths for launch tools.
Clear Git overrides and startup variables at the launcher boundary.
A fixture may set `GIT_DIR` again. The file guard still protects the live Git directory.

Mount approved source and Cargo inputs read-only. Use a private checkout when a test must edit source or Git state.
Use a private copy without shared writable inodes. A linked worktree or a hard link to live data cannot be a writable fixture.
Use a private Cargo output when the compiler or tests need writes. Fetch dependencies in trusted setup and pass the required inputs read-only.
Use a fresh proc mount. Hide alternate proc mounts and all routes to the old root.
Change the working directory into the new mount view before exec. Close old root and host directory descriptors.
Part 2 owns writable services and outside access through sockets. A read-only mount does not prevent a write that a service performs.

### FIFOs and devices

Linux permits I/O on existing FIFOs on read-only mounts. Reading a jobserver FIFO can also remove a token. [Linux VFS permission check](https://github.com/torvalds/linux/blob/v6.18/fs/namei.c#L512-L520)
Use a mount view with no host FIFOs. Expose immutable tool paths and stage mutable inputs as ordinary files, directories, and symlinks.
Exclude FIFOs, sockets, and device nodes from staged inputs. Keep mounted inputs immutable for the life of the test.
Detach the old root before candidate exec. A scan of the live filesystem cannot prove that a host FIFO will stay absent.
Use a private `/dev` with only approved devices such as `null`, `zero`, and `urandom`. Hide `/dev/steve` and its aliases.
Keep host jobserver paths and descriptors in the trusted supervisor. Strip `CARGO_MAKEFLAGS`, `MAKEFLAGS`, and `MFLAGS` from the child environment.
Use the existing supervisor to reserve and release host tokens. Pass only the admitted thread count to nextest.
Use a private fixture FIFO for tests that need a jobserver. Verify the private FIFO and refusal of opens on host endpoints with disposable fixtures.
Verify nextest admission and token return through the queue route after Builds releases the hold. [invoke.sh:64](../../scripts/lint/invoke.sh#L64), [invoke.sh:707](../../scripts/lint/invoke.sh#L707)

### Gate parent-death handoff

Run the trusted launcher as a direct child of the gate. Keep the gate PID in trusted host setup.
After scope registration, exec Bubblewrap in the same process with `--unshare-pid --proc /proc --die-with-parent`.
Keep the Bubblewrap reaper as PID 1. Bubblewrap uses its parent-death option for the monitor, the reaper, and the command.
The scope record lets recovery stop the job if a parent exits before Bubblewrap arms its parent-death signal.

Remove the inherited `MERGE_GATE_PARENT_PID` in the launcher before entering the namespace.
Run the candidate command as a direct child of the Bubblewrap reaper.
Set `MERGE_GATE_PARENT_PID=1` in the candidate environment before the first shim or interpreter starts.
Skip `SUITE_BOOTSTRAP` on the guarded path. The trusted launcher applies its startup rules before candidate exec.
`sitecustomize.py` still uses the variable to skip startup folder allocation. The launcher already supplies a lease.
`module_run.py` consumes the variable and watches a pidfd for namespace PID 1.
The module runner still sets a parent-death signal for its own test child.
The Bubblewrap monitor reports the candidate status. When PID 1 exits, the kernel stops all remaining namespace members.
[Gate bootstrap](../../scripts/production/merge_gate.py#L121), [Gate launch](../../scripts/production/merge_gate.py#L1086), [Python startup](../../scripts/lib/python_startup/sitecustomize.py#L220), [Module runner](../../scripts/test_times/module_run.py#L610), [PID namespaces](https://man7.org/linux/man-pages/man7/pid_namespaces.7.html), [Bubblewrap parent-death option](https://github.com/containers/bubblewrap/blob/v0.11.2/bubblewrap.c#L349-L374)

#### Cancellation

The existing gate code blocks cancellation signals during spawn and registration. The launcher inherits that mask.
After registration, exec the trusted GNU `env --default-signal` with the absolute Bubblewrap path as its command.
GNU `env` restores default signal dispositions and unblocks inherited signals before it execs Bubblewrap.
Bubblewrap keeps its built-in reaper. It unblocks its internal `SIGCHLD` mask before candidate exec.
Keep the default cancellation dispositions in PID 1 and the candidate.
Add no signal handlers, masks, or disposition changes in new Python or shell code, including fixtures.
On cancellation, timeout, or setup failure, write `1` to the verified job's `cgroup.kill`.
The kernel sends `SIGKILL` to every member, regardless of its mask or process group.
[GNU env implementation](https://github.com/coreutils/coreutils/blob/v9.10/src/env.c#L751-L754), [Bubblewrap candidate exec](https://github.com/containers/bubblewrap/blob/v0.11.2/bubblewrap.c#L3370-L3392)

#### Lease release and recovery

Use the [Part 2 scope layout](test-live-folder-guard-walls.md#cgroup-layout). Each job has its own host-owned cgroup v2.
Keep the launcher blocked until the gate verifies its cgroup placement and records the scope's invocation identity with the lease.
Authorize Bubblewrap start after the existing cancellation deferral ends.
If the setup channel closes before approval, exit before starting Bubblewrap. Keep the gate as the launcher's parent.
Hide cgroup control files from the candidate. Keep accounting and user manager requests on the host.
If the Bubblewrap monitor exits and the cgroup still has members, stop the remaining processes through `cgroup.kill`.
Reap the launcher on the live gate path, including when cancellation occurs before cgroup placement.
For normal exit, forced stop, and crash recovery, require `cgroup.events` to report `populated 0` before releasing the lease.
This proves that PID 1 and all descendants exited. Reaping the host launcher alone cannot give this proof.
Retain the lease and recovery record if the cgroup identity or empty state cannot be verified.
Require the same proof in the folder sweeper, even if a crash closes the lease lock.
Keep the cgroup available until the gate or recovery records the proof. Refuse launch if the required cgroup controls are unavailable.
[Gate recovery](../../scripts/production/merge_gate.py#L700), [Gate cleanup](../../scripts/production/merge_gate.py#L1000)

The `start_gate_job()` proof is pending. Use separate fixture launches through that entry point after Builds releases the hold.

| Case | Required result |
| --- | --- |
| Normal test module and type check | The candidate shim can write only to its lease. A wrong-root write in the shim fails. The gate receives the command status. |
| Python startup and module runner | A host PID in the input environment never reaches either reader. Startup uses the supplied lease. The module runner watches namespace PID 1. |
| Gate exits before scope registration | The launcher's setup channel closes. The launcher exits before starting Bubblewrap. |
| Gate or Bubblewrap monitor exits during Bubblewrap setup | Recovery stops any remaining members of the recorded cgroup before it releases the lease. |
| Gate exits or forces a stop after a fixture calls `setsid()` | Recovery waits for the cgroup to become empty after the launcher exits. The fixture process and PID 1 exit before lease release. |
| Cancellation during launch or after candidate start | The gate stops the recorded cgroup. The lease remains held until the cgroup is empty. |
| Candidate startup from the existing deferred spawn | The first exec receives default dispositions for `SIGTERM`, `SIGHUP`, and `SIGINT`. Its signal mask is empty. |
| Gate timeout | The gate reaps the host job and records its status. The lease remains held until the cgroup is empty. |

Represent a leased path as `PrivateTestFolder` after the launcher verifies its owner, mount, and lifetime.
Return `ProtectedTestProcess` or `TestIsolationUnavailable`. Include the failed setup operation in `TestIsolationUnavailable`.
Refuse the test if setup fails. An unavailable user namespace or build scope cannot permit an unguarded test.

### Natedev measurements

The probes ran on natedev on 2026-11-19. The host used UID 1000, Linux 6.18.50, util-linux 2.42.2, and systemd 260.2.
Each timing uses seven sequential starts of `true`. The median includes command startup.
The saved command output is in this natedev session transcript.

```text
/home/natepiano/.codex/sessions/2026/11/19/rollout-2026-11-19T08-36-04-01a1f405-844f-7aa2-b750-7ecf8791d2f5.jsonl
```

| Transcript lines | Saved output |
| --- | --- |
| 495 and 524 | Host mount sources, scope option refusal, tool versions, and plain mount namespace refusal. |
| 551 | Root-map mount flags, fixture write refusals, and private writes under the `/session` bind. |
| 586 and 589 | Timing command and output for root-map, scope, user service, and Landlock. The output includes the Git canaries. |
| 696 and 699 | Mapped-user command and output for timings, file canaries, proc mount, and the direct module run. |

The timing commands saved the count, median, minimum, and maximum. The individual timing samples are pending.

| Candidate | Median and range | Result |
| --- | --- | --- |
| Direct start in the mapped-user batch | 3.64 ms, 2.65–5.69 ms | Baseline. |
| Plain `unshare -m` | — | Refused with `EPERM`. |
| `unshare -rm` and recursive read-only mount | 27.22 ms, 22.30–38.74 ms | Works without host root. The child maps UID 1000 to UID 0. |
| Mapped-user mount and private proc | 18.25 ms, 15.62–64.18 ms | Works without host root. Keeps UID 1000. This was the first file guard prototype. |
| Current `systemd-run --user --scope` | 16.28 ms, 14.11–73.03 ms | The scope starts. It rejects `ReadOnlyPaths` as an unknown assignment. |
| Transient `systemd-run --user --pipe` service | 57.82 ms, 45.66–106.60 ms | `ProtectSystem=strict`, `ReadWritePaths`, and `ReadOnlyPaths` worked without host root. The service changes launch and recovery behavior. |
| Python Landlock probe, ABI 7 | 47.28 ms, 30.01–232.05 ms | Blocked append, truncate, and unlink with `EACCES`. The tested rules allowed `chmod`. |

The mapped-user prototype added 14.61 ms to the paired median.
Part 2 records the [Bubblewrap pilot timings](test-live-folder-guard-walls.md#natedev-timing-output). The integrated gate timing with Bubblewrap is pending.
The root-map probe and user service blocked a fixture Git commit at `index.lock` with `EROFS`.
The root-map batch timed out after five seconds on `test_process_failure.py`. Its result under the production launcher is pending.
The mapped-user probe blocked append, truncate, chmod, dirfd unlink, and a symlink alias with `EROFS`. A rename across the bind returned `EXDEV`.
The directory descriptor came from an open inside the guard. Closure of writable file and directory descriptors inherited from the host is pending.
Pass both descriptor types from a disposable host fixture. Verify they are closed before the candidate shim starts.
The probe wrote inside the private mount. `findmnt` reported `ro` for `/`, `/session`, `/build`, and the checkout.
The host mount source identifies `/session` as a bind of the `session` directory on `/build`. The host still reported `rw` for `/session`.
Mutation checks through both bind paths of the same disposable fixture are pending.
The mapped-user probe showed a private read-only `/proc`. A lookup of a known host process through `/proc/<pid>/root` is pending.
The mapped-user prototype passed one test from `scripts/lib/test_git_index.py` through `module_run.py` with private `TMPDIR`.

The Bubblewrap launcher sets `TMPDIR`, `HOME`, and XDG paths before exec. Apply the mount, device, descriptor, and parent-death rules above.
Check the bootstrap with `findmnt -T`, a disposable sibling canary, and `/proc/self/status`. Record setup errors separately from test failures.
Keep the existing build scope for accounting. Start it outside the file guard. Keep parent identity and recovery checks on the side that can see the host PIDs.
Record raw timing samples and proof output during the first authorized integration run. Verify all three guards in that launch.

## Test starts and repairs

The step scope can run a command directly if scope setup fails. Keep the file guard on the command through that fallback. [invoke.sh:165](../../scripts/lint/invoke.sh#L165)
Use one file guard for the paths below. Test each route through its normal entry point with a disposable wrong-root canary.

| Start path on the base | Required change or named gap |
| --- | --- |
| Landing merge tests, checkpoint tests, fast-track tests, and base-red reruns | Route leaf tests from `checkpoint_gate.run_test_command()` to the guard. Let gate and verify supervisors start their own guarded test children. Keep fetch, commit, and push in the controller. [checkpoint_gate.py:117](../../scripts/production/checkpoint_gate.py#L117), [merge_checkpoint.py:1304](../../scripts/production/merge_checkpoint.py#L1304), [merge_gate.py:1086](../../scripts/production/merge_gate.py#L1086) |
| Full gate, tests-only, selected modules, retries, type check, and `targeted_test.py` | Apply the guard at `start_gate_job()` before every candidate `<repo>/scripts/lib/py` exec. Include `TYPE_CHECK`. `targeted_test.py` calls the same runner. Keep the boundary if phase 355 moves the runner into `gate.py`. [merge_gate.py:1086](../../scripts/production/merge_gate.py#L1086), [merge_gate.py:1722](../../scripts/production/merge_gate.py#L1722), [targeted_test.py:17](../../scripts/production/targeted_test.py#L17) |
| Nightly merge gate | Guard each module and type-check job in the nightly clone through `run_suites()`. Keep clone setup, notification, and slot sweep in the controller. [merge_gate.py:2563](../../scripts/production/merge_gate.py#L2563), [merge_gate.py:2730](../../scripts/production/merge_gate.py#L2730) |
| Queue-started test, lint wrapper, local `verify.sh`, and token-limited nextest | Select the guard before the test command reaches `buildlog_exec()` or the queue fallback. Keep the wrapper on the command when scopes are unavailable. Put nextest execution and its test subprocesses behind the guard. [queue_place.py:196](../../scripts/build_queue/queue_place.py#L196), [invoke.sh:774](../../scripts/lint/invoke.sh#L774), [verify.sh:3305](../../scripts/direct/verify.sh#L3305), [verify.sh:4815](../../scripts/direct/verify.sh#L4815) |
| Worker pane, manual session, and detached run | Provide a protected test shell for arbitrary commands. Connect worker and session test launches to it. Wrap detached test commands before the target shell starts. Detached accounting alone has no file rules. [worker_panes.py:1404](../../scripts/herdr/worker_panes.py#L1404), [pane_launch.py:1073](../../scripts/herdr/pane_launch.py#L1073), [detached_runs.py:1292](../../scripts/agents/detached_runs.py#L1292) |
| Raw `python -m unittest`, `pytest`, direct Python, `python -I`, or direct `module_run.py` | Start the interpreter inside the protected shell. The Python shim only makes a temporary folder for certain command shapes. It cannot enforce protection for another interpreter or before import side effects. [sitecustomize.py:220](../../scripts/lib/python_startup/sitecustomize.py#L220), [module_run.py:516](../../scripts/test_times/module_run.py#L516) |
| Raw `cargo test`, `cargo nextest run`, a test executable, shell tests, and hook checks | Use the protected shell for the entire command. Match the route by its execution boundary. A filename filter cannot find every test program. [test_agent_exec.sh](../../scripts/agents/test_agent_exec.sh), [test_cancel_prior.sh](../../scripts/validate_and_push/test_cancel_prior.sh), [BERTH hook checks](../../scripts/berth/install/README.md#L44) |
| `module_cost.py` time run and trace run | Guard both module starts from `measure()`, before `task_cpu.exec_stage()` executes the candidate module runner. Keep profiling and its lease lock in the trusted supervisor. Check both trace attribution and test results after integration. [module_cost.py:94](../../scripts/test_times/module_cost.py#L94), [task_cpu.py:217](../../scripts/test_times/task_cpu.py#L217) |
| `coverage_overlap.py` | Start its interpreter inside the guard. It imports and runs the suite in that interpreter. Keep `COVERAGE_OUT` inside the lease and collect its result in the supervisor. [coverage_overlap.py:219](../../scripts/test_times/coverage_overlap.py#L219), [coverage_overlap.py:328](../../scripts/test_times/coverage_overlap.py#L328) |
| `memory_page_proof.mjs` from the gate or a manual command | Guard Node before it starts Python for fixtures or starts Chrome. Inherit the test module's guard for calls from the gate. Use the protected shell for direct runs. Put the short Chrome profile, fixture output, and screenshots under the lease. [memory_page_proof.mjs:52](../../scripts/build_queue/memory_page_proof.mjs#L52), [memory_page_proof.mjs:158](../../scripts/build_queue/memory_page_proof.mjs#L158), [test_memory_feed.py:1131](../../scripts/build_queue/test_memory_feed.py#L1131), [test_memory_feed.py:3254](../../scripts/build_queue/test_memory_feed.py#L3254) |
| Mac offload, Mac queue, and another SSH runner | Part 2 must apply the guard on the execution host before the remote command starts. The local scope has no effect there. [offload.py:2024](../../scripts/mac_test/offload.py#L2024), [mac_queue_run.py:95](../../scripts/mac_test/mac_queue_run.py#L95), [linux_runner.py:691](../../scripts/mac_test/linux_runner.py#L691) |
| AWS production and trial gates, paired trial, and Hana nextest | Part 2 must guard candidate suites, the paired trial's direct module spawn, and the Hana guest command. [gate_box.py:116](../../scripts/aws_target/gate_box.py#L116), [gate_box.py:243](../../scripts/aws_target/gate_box.py#L243), [hana_runner_entry.py:17](../../scripts/aws_target/hana_runner_entry.py#L17), [gpu_trial_wrapper.py:16](../../scripts/aws_target/gpu_trial_wrapper.py#L16) |

The type-check job passes the caller's `BASH_ENV` path to the candidate shim when the variable is set. The gate supplies no fixed path.
The job copies the inherited environment, then changes `TMPDIR` and removes the memory tripwire.
Bash can source the file named by `$BASH_ENV` before the shim body. The variable is unset in the current natedev shell.
Clear `BASH_ENV` before the first trusted shell and before candidate exec. Use a disposable `$private_test_folder/gate-bash-env.sh` to verify that Bash skips it.
Verify that both the candidate shim and Python startup run only after the file guard applies. [merge_gate.py:1571](../../scripts/production/merge_gate.py#L1571), [merge_gate.py:1722](../../scripts/production/merge_gate.py#L1722), [scripts/lib/py](../../scripts/lib/py)

An unrestricted host shell can still start any interpreter. This is an open gap until repository test sessions cannot obtain that execution path.
Move raw test sessions to a dedicated execution profile. Give them protected or disposable checkouts and deny access to the live account's write authority.
Keep trusted operator actions in separate entry points. Route a terminal test command through the guarded shell even when it uses an absolute interpreter path.
Do not call coverage complete until the session and worker launch configuration enforces this boundary. A command alias or a pre-tool hook alone cannot enforce it.

## Delete paths that choose live roots

Require an explicit root at the deleting library boundary. Resolve production defaults in a trusted command entry point.
Pass fixture roots from tests. Preserve hold, ownership, lease, and stale-process checks when moving a default.
The queue owns its sweep guard. It must refuse an unproved run root even when the file guard would block the write.

| Live default or root discovery | Deleting paths to change or verify |
| --- | --- |
| Temp sweep and cleanup plans | `default_roots()` and `run()` can select real temp, run, scratch, and copy roots. `main()` applies a plan and runs expiry cleanup. Require explicit roots before reaching `apply_cleanup_plan()` or `delete_tree()`. [tmp_sweep.py:381](../../scripts/lint/tmp_sweep.py#L381), [tmp_sweep.py:665](../../scripts/lint/tmp_sweep.py#L665), [tmp_sweep.py:1462](../../scripts/lint/tmp_sweep.py#L1462), [tmp_sweep.py:1578](../../scripts/lint/tmp_sweep.py#L1578), [tmp_sweep.py:1645](../../scripts/lint/tmp_sweep.py#L1645) |
| Queue startup and temp folders | `cleanup_queue_place_folders()` selects the inherited temp parent. `cleanup_program_starts()` resolves its records from live admission state. Pass each root explicitly. [queue_place.py:192](../../scripts/build_queue/queue_place.py#L192), [queue_place.py:220](../../scripts/build_queue/queue_place.py#L220), [queue_place.py:236](../../scripts/build_queue/queue_place.py#L236) |
| Test folder bootstrap and old gate folders | `test_run_folder()` sweeps a parent from the caller's temp environment. Pass the guard's private parent. Gate cleanup can remove leased temp folders. Keep gate recovery rooted in its own exact lease parent. [testing_folders.py:20](../../scripts/lib/testing_folders.py#L20), [build_folders.py:958](../../scripts/lib/build_folders.py#L958), [merge_gate.py:763](../../scripts/production/merge_gate.py#L763), [merge_gate.py:1164](../../scripts/production/merge_gate.py#L1164) |
| Test temp files under an inherited `TMPDIR` | Close the `/session/queue` write path before any import. `test_run_folder()` uses the inherited temp parent. The staff fixture allocates at import. The unit-runs test creates fixture run trees under the Python temp parent. Verify all three with a disposable caller temp root and a private lease. [testing_folders.py:28](../../scripts/lib/testing_folders.py#L28), [staff_test_support.py:45](../../scripts/production/staff_test_support.py#L45), [test_unit_runs.py:267](../../scripts/lib/test_unit_runs.py#L267) |
| Build outputs, disk floors, and doc indexes | `sweep_workspace()`, `hold_floor()`, and `recover_pending_trims()` can derive live roots from config. Leaf deletion uses `remove_entry_at()` and `prune_doc_index()`. Require explicit targets for the mutating call. [sweep.py:2234](../../scripts/lint/sweep.py#L2234), [sweep.py:2324](../../scripts/lint/sweep.py#L2324), [sweep.py:2485](../../scripts/lint/sweep.py#L2485), [sweep.py:4122](../../scripts/lint/sweep.py#L4122), [sweep.py:5218](../../scripts/lint/sweep.py#L5218) |
| Scratch and copy task cleanup, and slot sweep | `scratch.end()` uses configured task and copy roots. `sweep_slots()` loads slot and Cargo roots. Give the task or slot operation an explicit root set. [scratch.py:2381](../../scripts/lib/scratch.py#L2381), [build_slots.py:446](../../scripts/lib/build_slots.py#L446), [copies.py:1628](../../scripts/copies/copies.py#L1628) |
| Folder audit, gate folder cleanup, and Git prune | `folder_audit.main()` constructs roots for removal. `resume_pending_prunes()` uses configured state. `clean_checkout_cargo_output()` derives outputs from the checkout. Gate cleanup and worktree cleanup must use the selected roots and private Git context. [folder_audit.py:394](../../scripts/lint/folder_audit.py#L394), [folder_audit.py:681](../../scripts/lint/folder_audit.py#L681), [folder_audit.py:757](../../scripts/lint/folder_audit.py#L757), [gate_folders_delete.py:156](../../scripts/lint/gate_folders_delete.py#L156), [worktrees.py:273](../../scripts/worktree/worktrees.py#L273) |
| Production state and notes | `tidy()` selects live state from module defaults. `prune_all()` selects live timer owners and their documents. Move selection to the caller. [tidy_wrapped.py:33](../../scripts/production/tidy_wrapped.py#L33), [tidy_wrapped.py:216](../../scripts/production/tidy_wrapped.py#L216), [prune_phase_notes.py:22](../../scripts/production/prune_phase_notes.py#L22) |
| Claude account temp files | `sweep_claude_temp_files()` unlinks old `.claude.json.tmp.*` files in the live home and its numbered Claude homes. Pass an explicit root. Keep the age, file type, and no-symlink checks. [accounts.py:3689](../../scripts/accounts/accounts.py#L3689) |

Find removal sites with `rg -n 'rmtree\(|\.unlink\(|\brmdir\(|sweep|prune|cleanup|git clean' scripts -g '*.py' -g '*.sh'`.
Follow each selected path to its root resolver. An explicit leaf argument does not make a live root default safe upstream.
Check new hits when the base changes. For example, `prune_finished(runs)` already takes its root. Verify its callers. [detached_runs.py:712](../../scripts/agents/detached_runs.py#L712)

## First report-only run

Wait for Builds to release the hold. This run will collect the measurement after the file guard exists.
Give natemerge priority. Follow the run order Builds sets after natemerge finishes.
Report-only means the results do not block landing yet. Keep the write protection on throughout the run.
Use the existing gate's discovery and module runner. Add the tracked shell tests and the BERTH hook check, which gate discovery omits.
At `81ac379ca`, `git ls-files` counted 867 tracked `scripts/**/test_*.py` files and five tracked `test_*.sh` files.
Add `node scripts/build_queue/memory_page_proof.mjs --gate` with a real Chrome and private output as a distinct census entry.
Keep the gate module that tests real and fixture Chrome in the manifest. Mark the direct browser entry as incomplete if Chrome is unavailable.
Include a guarded type-check job and checks for both `module_cost.py` stages and the `coverage_overlap.py` interpreter.
Record the manifest at the tested SHA. Record any additional commands from actual start paths separately.
Repair `MergeGateFixture.setUpClass()` before the census. Replace `TemporaryDirectory(prefix="gate-config-", dir="/tmp")` with a folder under the verified private parent.
Set the fixture's `roots.gate` in that class config before selecting its tmpfs parent. Apply the same repair to callers of the class setup.
Keep the fixture's short socket paths inside the lease. [merge_gate_test_support.py:319](../../scripts/production/merge_gate_test_support.py#L319), [build_folders.py:197](../../scripts/lib/build_folders.py#L197)
Use a detached run at normal admission limits. Use no CPU load generator. Make each module's writable folder fresh.
Trace mutating file calls with descendants and decoded file descriptors. Keep syscall, path, PID, and errno.
Include creates, writes, truncates, rename and link pairs, deletion, and metadata changes. Include relative paths and directory-descriptor paths.
Record attempted writes outside each module's folder, including requests that fail before lookup. Separate shared fixture setup, cases, and teardown.
Record guard setup refusals, import failures, and class setup failures apart from test failures and later write attempts.
Mark cases skipped by failed setup as unobserved. Rerun those cases after fixture repair before reporting their write results.
Use module names for attribution first. Rerun affected cases with case names and timestamps if the trace cannot identify the caller.
Bound raw output per module, retain a compact summary, and delete raw output before the next run. Report dropped records as incomplete.
Record module exit status and counts for protected, refused setup, traced violations, and missing results. Report holds and interruptions as incomplete.
An empty violation list only counts when every manifest entry has a result. Change fixtures for legitimate writes. Never broaden the host write grant.
Verify a wrong-root unlink and a Git commit against disposable fixtures through each real start path before making the guard mandatory.

## Build phases and cost

These are worker-hour estimates. Measure elapsed run time and launcher cost again after integration. Time starts after bob approves the plan.

| Phase | Worker hours | ETA and completion evidence |
| --- | ---: | --- |
| Implement the launcher and lease integration | 5–7 | One workday. Prove writes fail before test imports, private writes work, and failed setup stops the command. |
| Connect local start paths and remove live root defaults | 8–12 | Two more workdays. Verify landing, retry, nightly, queue, worker, and raw terminal routes. Coordinate the queue sweep guard with Queue. |
| Run the census and repair fixtures | 4–8 initially | Start after Builds releases the hold. Allow one workday for the first run and report. Estimate the remaining repairs from the results. |
| Require the file guard on natedev | 3–5 | One workday after fixture repairs and the session bypass repair. Verify the manifest and the one-canary proof for every route. |

The initial estimate is 20–32 worker hours. The full-suite result, the enforced session profile, and Part 2 determine the final rollout date.

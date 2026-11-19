# List item 141 — A test cannot delete or change live folders

This is Part 1 of phase 389. The producer and reviewer is `bob`. The plan is due on 2026-11-19 at 15:00 EST.

Cover every test in `claude_commands` on natedev, across all productions and units. Include manual tests and every descendant of a test.
Part 2 covers process visibility, outside access, and the Mac and AWS forms. The start inventory below includes the remote paths.

On 2026-11-19 at 12:46 EST, a queue sweep test used a live default and deleted 11 entries in `/tmp/claude/unit`.
On 2026-11-14, an inherited `GIT_DIR` let fixture commits reach live `~/.claude` main.
A wrong path can come from an argument, an environment variable, a config file, or a direct absolute path.
The kernel must refuse the write even when the caller chooses the wrong path.

## Live roots on natedev

Treat all host files as read-only in a test. The list records the roots and aliases that the file guard must verify.

| Root | Evidence at base `81ac379ca` |
| --- | --- |
| `/tmp/claude`, including `unit`, `delegate`, and `astra` | `TMP_ROOT`, `default_roots()`, and `run_roots()` select these roots. `stat` confirmed `/tmp/claude`. [tmp_sweep.py:134](../../scripts/lint/tmp_sweep.py#L134), [unit_runs.py:33](../../scripts/lib/unit_runs.py#L33) |
| `/tmp`, `/var/tmp`, `/tmp/claude-1000`, and `/dev/shm` | `stat` confirmed all four. The sweep lists per-user scratch under `/tmp`. The gate uses `/dev/shm/merge-gate-tests`. [build_folders.conf:10](../../config/build_folders.conf#L10), [tmp_sweep.py:410](../../scripts/lint/tmp_sweep.py#L410), [merge_gate.py:1407](../../scripts/production/merge_gate.py#L1407) |
| `/session`, `/build/session`, and `~/session` | `findmnt -T /session` reported `/dev/mapper/build[/session]`. The config selects `/session` here and `~/session` on other hosts. `~/session` was absent here. [build_folders.conf:27](../../config/build_folders.conf#L27) |
| `/build`, including `claude/scratch`, `claude/copies`, `cargo`, `paths`, `slot`, and `hana-ci` | `findmnt -T /build` reported its own writable ext4 mount. The build folder and lint configs name these folders. [build_folders.conf:13](../../config/build_folders.conf#L13), [lint.conf:197](../../config/lint.conf#L197) |
| `~/.claude`, `~/worktrees`, and `~/rust`, including `~/rust/hanadocs` | `stat` confirmed the live checkout, worktree root, and vault. `git rev-parse --git-common-dir` resolved this worktree to `~/.claude/.git`. The config names planned shell and mirror roots under `~/rust`. [build_folders.conf:24](../../config/build_folders.conf#L24), [staff.conf:13](../../config/staff.conf#L13) |
| `~/.local/state`, `~/.config`, `~/.cache`, `~/.codex`, `~/.cargo`, and `~/.rustup` | `stat` confirmed these roots. Staff and build log code select live state here. The Codex quota reader selects `~/.codex/sessions`. [staff.py:815](../../scripts/production/staff.py#L815), [store.py:46](../../scripts/buildlog/store.py#L46), [codex_pacer.py:12](../../scripts/whoami/codex_pacer.py#L12), [lint.conf:194](../../config/lint.conf#L194) |
| `/run`, including `/run/user/1000`, plus `/var/lib/hana-ci` | `stat` confirmed the user runtime directory. The user manager provides `XDG_RUNTIME_DIR`. Lint names the CI roots. [invoke.sh:603](../../scripts/lint/invoke.sh#L603), [lint.conf:197](../../config/lint.conf#L197) |
| Any absolute or configured path outside the leased test folder | The inherited temporary parent can come from `TMPDIR`. The mounted root covers later roots and aliases without another config edit. [build_folders.py:192](../../scripts/lib/build_folders.py#L192) |

An environment override can select another data root. It cannot grant write access.
A bind mount can expose the same files at two paths. Check the mount tree, including child mounts, before starting the command.

## One file guard at test start

Use a shared natedev launcher. Put the file guard after trusted setup and before the first interpreter, shell, test binary, or candidate build script.

1. Allocate one leased folder with the build folder maker.
2. Create the run's `TMPDIR`, `HOME`, XDG directories, logs, and writable fixtures inside it.
3. Select the source checkout and any read-only build inputs.
4. Start `unshare` with a private mount view and a fresh `/proc` for the file boundary.
5. Make the mount tree recursively read-only.
6. Bind only the new leased folder writable.
7. Remove capabilities and set `no_new_privs` before executing the command.
8. Send output through pipes to the trusted parent.
9. Release the folder when its owner and all test descendants exit.

Reuse `test_run_folder()` and the existing build folder leases. Use an explicit parent from the supervisor, inside the new writable mount.
Keep `BUILDLOG_DIR`, the session fixture root, Python bytecode, and browser profiles inside the lease.
Do not reuse the caller's `DEVTOOLS_TEST_RUN_FOLDER` or `TMPDIR` as a writable mount.
Keep run output and worker records in the trusted parent. Keep one new folder per invocation and one private child folder per gate module.
Keep recovery and lease ownership outside the test. Review `pass_fds=copies.COMMAND_LOCKS.get()` when phase 383 moves gate recovery.
A delegate can keep a lease in a trusted supervisor. It must not pass a writable host file or a host directory descriptor to the command.
Use the phase 388 environment allow list before any test shell runs. Clear Git overrides and startup variables at the launcher boundary.
A fixture may set `GIT_DIR` again. The file guard still protects the live Git directory.

Mount the source and shared Cargo artifacts read-only. Use a private checkout when a test must edit source or Git state.
Use a private copy without shared writable inodes. A linked worktree or a hard link to live data cannot be a writable fixture.
Use a private Cargo output when the compiler or tests need writes. Fetch dependencies in trusted setup and pass the required inputs read-only.
Create only private device and temporary mounts where a test needs them. Never restore a writable host parent for one module.
Use a fresh proc mount to prevent access to a host mount through `/proc/<pid>/root`.
Hide alternate proc mounts and host devices that can write to storage. Close inherited host file handles before launch.
Part 2 owns writable services and outside access through sockets. A read-only mount does not prevent a write that a service performs.

Represent a leased path as `PrivateTestFolder` after the launcher verifies its owner, mount, and lifetime.
Return `ProtectedTestProcess` or `TestIsolationUnavailable`. Include the failed setup operation in `TestIsolationUnavailable`.
Refuse the test if setup fails. An unavailable user namespace or build scope cannot permit an unguarded test.

### Natedev measurements

The probes ran on natedev on 2026-11-19. The host used UID 1000, Linux 6.18.50, util-linux 2.42.2, and systemd 260.2.
Each timing uses seven sequential starts of `true`. The median includes command startup.

| Candidate | Median and range | Result |
| --- | --- | --- |
| Direct start in the mapped-user batch | 3.64 ms, 2.65–5.69 ms | Baseline. |
| Plain `unshare -m` | — | Refused with `EPERM`. |
| `unshare -rm` and recursive read-only mount | 27.22 ms, 22.30–38.74 ms | Works without host root. The child maps UID 1000 to UID 0. |
| Mapped-user mount and private proc | 18.25 ms, 15.62–64.18 ms | Works without host root. Keeps UID 1000. Selected for Part 1. |
| Current `systemd-run --user --scope` | 16.28 ms, 14.11–73.03 ms | The scope starts. It rejects `ReadOnlyPaths` as an unknown assignment. |
| Transient `systemd-run --user --pipe` service | 57.82 ms, 45.66–106.60 ms | `ProtectSystem=strict`, `ReadWritePaths`, and `ReadOnlyPaths` worked without host root. The service changes launch and recovery behavior. |
| Python Landlock probe, ABI 7 | 47.28 ms, 30.01–232.05 ms | Blocked append, truncate, and unlink with `EACCES`. The tested rules allowed `chmod`. |

The mapped-user launcher added 14.61 ms to the paired median. The timing does not include folder setup, test startup, or production integration.
The root-map probe and user service blocked a fixture Git commit at `index.lock` with `EROFS`.
The mapped-user probe blocked append, truncate, chmod, dirfd unlink, and a symlink alias with `EROFS`. A rename across the bind returned `EXDEV`.
The probe wrote inside the private mount. `findmnt` reported `ro` for `/`, `/session`, `/build`, and the checkout. The host still reported `rw` for `/session`.
The selected file guard passed `scripts/lib/test_git_index.py` through `module_run.py` with private `TMPDIR`. It ran one test.

The tested bootstrap uses a new folder created by the trusted caller. Pass the folder as the first argument and the test command after it.

```bash
unshare --map-current-user --keep-caps --mount --pid --fork --kill-child \
  --mount-proc --propagation private -- sh -eu -c '
    folder=$1
    shift
    mount -o remount,bind,ro=recursive /
    mount --bind "$folder" "$folder"
    mount -o remount,bind,rw,nosuid,nodev "$folder"
    exec setpriv --no-new-privs --bounding-set=-all --inh-caps=-all --ambient-caps=-all "$@"
  ' test-file-guard "$private_test_folder" ./scripts/lib/py scripts/test_times/module_run.py scripts/lib test_git_index.py
```

The launcher sets `TMPDIR`, `HOME`, and XDG paths before this command. The production launcher must verify the mount and descriptor rules before the final exec.
Check the bootstrap with `findmnt -T`, a disposable sibling canary, and `/proc/self/status`. Record setup errors separately from test failures.
Keep the existing build scope for accounting. Start it outside the file guard. Keep parent identity and recovery checks on the side that can see the host PIDs.
The measured proc mount protects the file boundary. Part 2 will verify the full process and outside-access rules.

## Test starts and repairs

The step scope can run a command directly if scope setup fails. Keep the file guard on the command through that fallback. [invoke.sh:165](../../scripts/lint/invoke.sh#L165)
Use one file guard for the paths below. Test each route through its normal entry point with a disposable wrong-root canary.

| Start path on the base | Required change or named gap |
| --- | --- |
| Landing merge tests, checkpoint tests, fast-track tests, and base-red reruns | Route leaf tests from `checkpoint_gate.run_test_command()` to the guard. Let gate and verify supervisors start their own guarded test children. Keep fetch, commit, and push in the controller. [checkpoint_gate.py:117](../../scripts/production/checkpoint_gate.py#L117), [merge_checkpoint.py:1304](../../scripts/production/merge_checkpoint.py#L1304), [merge_gate.py:1086](../../scripts/production/merge_gate.py#L1086) |
| Full gate, tests-only, selected modules, retries, and `targeted_test.py` | Apply the guard at `start_gate_job()` for test modules. `targeted_test.py` calls the same runner. Keep both entry names if phase 355 moves it into `gate.py`. [merge_gate.py:1415](../../scripts/production/merge_gate.py#L1415), [merge_gate.py:2714](../../scripts/production/merge_gate.py#L2714), [targeted_test.py:17](../../scripts/production/targeted_test.py#L17) |
| Nightly merge gate | Guard each module in the nightly clone through `run_suites()`. Leave clone setup, notification, and the real slot sweep in the controller. [merge_gate.py:2563](../../scripts/production/merge_gate.py#L2563), [merge_gate.py:2730](../../scripts/production/merge_gate.py#L2730) |
| Queue-started test, lint wrapper, local `verify.sh`, and token-limited nextest | Select the guard before the test command reaches `buildlog_exec()` or the queue fallback. Keep the wrapper on the command when scopes are unavailable. Put nextest execution and its test subprocesses behind the guard. [queue_place.py:196](../../scripts/build_queue/queue_place.py#L196), [invoke.sh:774](../../scripts/lint/invoke.sh#L774), [verify.sh:3305](../../scripts/direct/verify.sh#L3305), [verify.sh:4815](../../scripts/direct/verify.sh#L4815) |
| Worker pane, manual session, and detached run | Provide a protected test shell for arbitrary commands. Connect worker and session test launches to it. Wrap detached test commands before the target shell starts. Detached accounting alone has no file rules. [worker_panes.py:1404](../../scripts/herdr/worker_panes.py#L1404), [pane_launch.py:1073](../../scripts/herdr/pane_launch.py#L1073), [detached_runs.py:1292](../../scripts/agents/detached_runs.py#L1292) |
| Raw `python -m unittest`, `pytest`, direct Python, `python -I`, or direct `module_run.py` | Start the interpreter inside the protected shell. The Python shim only makes a temporary folder for certain command shapes. It cannot enforce protection for another interpreter or before import side effects. [sitecustomize.py:220](../../scripts/lib/python_startup/sitecustomize.py#L220), [module_run.py:516](../../scripts/test_times/module_run.py#L516) |
| Raw `cargo test`, `cargo nextest run`, a test executable, shell tests, and hook checks | Use the protected shell for the entire command. Match the route by its execution boundary. A filename filter cannot find every test program. [test_agent_exec.sh](../../scripts/agents/test_agent_exec.sh), [test_cancel_prior.sh](../../scripts/validate_and_push/test_cancel_prior.sh), [BERTH hook checks](../../scripts/berth/install/README.md#L44) |
| Mac offload, Mac queue, and another SSH runner | Part 2 must apply the guard on the execution host before the remote command starts. The local scope has no effect there. [offload.py:2024](../../scripts/mac_test/offload.py#L2024), [mac_queue_run.py:95](../../scripts/mac_test/mac_queue_run.py#L95), [linux_runner.py:691](../../scripts/mac_test/linux_runner.py#L691) |
| AWS production and trial gates, paired trial, and Hana nextest | Part 2 must guard candidate suites, the paired trial's direct module spawn, and the Hana guest command. [gate_box.py:116](../../scripts/aws_target/gate_box.py#L116), [gate_box.py:243](../../scripts/aws_target/gate_box.py#L243), [hana_runner_entry.py:17](../../scripts/aws_target/hana_runner_entry.py#L17), [gpu_trial_wrapper.py:16](../../scripts/aws_target/gpu_trial_wrapper.py#L16) |

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
| Build outputs, disk floors, and doc indexes | `sweep_workspace()`, `hold_floor()`, and `recover_pending_trims()` can derive live roots from config. Leaf deletion uses `remove_entry_at()` and `prune_doc_index()`. Require explicit targets for the mutating call. [sweep.py:2234](../../scripts/lint/sweep.py#L2234), [sweep.py:2324](../../scripts/lint/sweep.py#L2324), [sweep.py:2485](../../scripts/lint/sweep.py#L2485), [sweep.py:4122](../../scripts/lint/sweep.py#L4122), [sweep.py:5218](../../scripts/lint/sweep.py#L5218) |
| Scratch and copy task cleanup, and slot sweep | `scratch.end()` uses configured task and copy roots. `sweep_slots()` loads slot and Cargo roots. Give the task or slot operation an explicit root set. [scratch.py:2381](../../scripts/lib/scratch.py#L2381), [build_slots.py:446](../../scripts/lib/build_slots.py#L446), [copies.py:1628](../../scripts/copies/copies.py#L1628) |
| Folder audit, gate folder cleanup, and Git prune | `folder_audit.main()` constructs roots for removal. `resume_pending_prunes()` uses configured state. `clean_checkout_cargo_output()` derives outputs from the checkout. Gate cleanup and worktree cleanup must use the selected roots and private Git context. [folder_audit.py:394](../../scripts/lint/folder_audit.py#L394), [folder_audit.py:681](../../scripts/lint/folder_audit.py#L681), [folder_audit.py:757](../../scripts/lint/folder_audit.py#L757), [gate_folders_delete.py:156](../../scripts/lint/gate_folders_delete.py#L156), [worktrees.py:273](../../scripts/worktree/worktrees.py#L273) |
| Production state and notes | `tidy()` selects live state from module defaults. `prune_all()` selects live timer owners and their documents. Move selection to the caller. [tidy_wrapped.py:33](../../scripts/production/tidy_wrapped.py#L33), [tidy_wrapped.py:216](../../scripts/production/tidy_wrapped.py#L216), [prune_phase_notes.py:22](../../scripts/production/prune_phase_notes.py#L22) |

Find removal sites with `rg -n 'rmtree\(|\.unlink\(|\brmdir\(|sweep|prune|cleanup|git clean' scripts -g '*.py' -g '*.sh'`.
Follow each selected path to its root resolver. An explicit leaf argument does not make a live root default safe upstream.
Check new hits when the base changes. For example, `prune_finished(runs)` already takes its root. Verify its callers. [detached_runs.py:712](../../scripts/agents/detached_runs.py#L712)

## First report-only run

Wait for Builds to release the hold. This run will collect the measurement after the file guard exists.
Report-only means the results do not block landing yet. Keep the write protection on throughout the run.
Use the existing gate's discovery and module runner. Add the tracked shell tests and the BERTH hook check, which gate discovery omits.
At `81ac379ca`, `git ls-files` counted 867 tracked `scripts/**/test_*.py` files and five tracked `test_*.sh` files.
Record the manifest at the tested SHA. Record any additional commands from actual start paths separately.
Use a detached run at normal admission limits. Use no CPU load generator. Make each module's writable folder fresh.
Trace mutating file calls with descendants and decoded file descriptors. Keep syscall, path, PID, and errno.
Include creates, writes, truncates, rename and link pairs, deletion, and metadata changes. Include relative paths and directory-descriptor paths.
Record attempted writes outside each module's folder, including requests that fail before lookup. Separate shared fixture setup, cases, and teardown.
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

# List item 141 — Three walls for every test

Phase 389. Bob owns plan review. The plan is due on 2026-11-19 at 15:00 EST.
Cover every `claude_commands` test and its descendants across all productions and units. Include manual runs and remote execution.

## Summary for bob

| Wall | Protection | Cost evidence |
| --- | --- | --- |
| Files | Block writes to live folders and Git state. Give each job private writable storage. | The earlier mapped-user prototype added 14.61 ms per start. The Bubblewrap cost for this wall is pending. |
| Processes | Hide host PIDs. Keep each job's descendants in a cgroup that the supervisor can stop and verify. | The systemd PID prototype added 9.275 ms to the service median. The Bubblewrap cost for this wall is pending. |
| Outside access | Block host services, credential sockets, and external network access. Allow approved private fixtures. | The systemd network prototype added 12.590 ms to the service median. The Bubblewrap cost for this wall is pending. |

On natedev, Bubblewrap with all three walls and seccomp took 40.309 ms per launch at the median. Direct start took 5.013 ms.
The 35.296 ms difference is about 31 seconds of summed launch time for 867 modules. The integrated gate time is pending.
Mac guest costs and AWS launch costs are pending. The measured Mac times cover only `sandbox-exec`.
The [code phases](#code-phases-and-eta) total 53–88 worker hours, or about 7–11 workdays in sequence. Queue time can extend the schedule.

## Why the guards are required

On 2026-11-19 at 12:46 EST, a queue sweep test used a live default and deleted 11 entries in `/tmp/claude/unit`.
On 2026-11-14, an inherited `GIT_DIR` let fixture commits reach live `~/.claude` main.
Builds reported a test run that used the inherited `TMPDIR` and wrote under `/session/queue`.
Arguments, environment variables, config files, and absolute paths can select the wrong root. The kernel must enforce the write boundary.

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

## Natedev — One Bubblewrap launcher

Start every candidate interpreter, shell, test binary, and build script inside the guard. Keep trusted staging and admission on the host.
Bubblewrap provides the mount layout, namespaces, PID 1 reaper, and syscall filters in one launch.
Use absolute paths to trusted tools. Apply the phase 388 environment allow list before the first launcher exec.
Clear Git overrides and startup variables, including `BASH_ENV`.
Send stdout and stderr through pipes. Use a pipe or private device for stdin.
Close every inherited descriptor other than the selected standard streams before candidate exec.
Use a private user, mount, PID, IPC, cgroup, network, and UTS namespace. Use `--new-session`.
Drop all capabilities with `--cap-drop ALL`. Bubblewrap sets `no_new_privs`.
Block `setns`. Disable nested user namespaces except for an approved private mount fixture.
Verify placement, resource limits, and the three walls before candidate exec. Refuse a launch if any required guard is unavailable. Return `ProtectedTestProcess` after a verified start.
[Gate start](../../scripts/production/merge_gate.py#L1086), [Type check](../../scripts/production/merge_gate.py#L1722), [Python shim](../../scripts/lib/py)

### Files

1. Allocate one leased folder per invocation and a child folder per gate module. Verify its owner, mount, and lifetime as `PrivateTestFolder`.
2. Start with an empty root. Expose only approved read-only inputs with `--ro-bind`.
3. Use `--bind` for the job's leased storage. Limit writable storage with a tmpfs size or disk quota.
4. Give the job private `HOME`, XDG directories, shared memory, and a fallback `/tmp`. Supply synthetic passwd and group files.
5. Change to the guarded working directory and apply `--remount-ro /`. Hide the old root and alternate proc mounts.

Replace `TMPDIR`, `TMP`, and `TEMP` before interpreter startup. Clear the inherited `DEVTOOLS_TEST_RUN_FOLDER`.
Keep `BUILDLOG_DIR`, session fixtures, bytecode, browser profiles, and fixture sockets within the storage limit.
Pass the private parent to `test_run_folder()` and other allocation or sweep helpers. Keep nested and sibling allocations inside the lease.
Resolve fixture PIDs in the job's PID namespace. Recreate the private paths for every local and remote job.
Keep output records, lease ownership, and `copies.COMMAND_LOCKS` with the supervisor. Coordinate lock ownership with phase 383.
Fetch dependencies in trusted setup. Give tests private Cargo output and a private Git checkout when they need writes.
Use copies without shared writable inodes. Keep linked worktrees and hard links to live data outside writable fixtures.
A visible read-only path rejects writes with `EROFS`. A hidden live path is absent and gets `ENOENT`.
[Temp folder setup](../../scripts/lib/testing_folders.py#L28), [Staff fixture import](../../scripts/production/staff_test_support.py#L45), [Unit-run fixtures](../../scripts/lib/test_unit_runs.py#L267)

#### FIFOs and devices

Expose immutable tool paths. Stage mutable inputs as ordinary files, directories, and symlinks.
Exclude FIFOs, sockets, and device nodes from staged inputs. Keep mounted inputs immutable for the test.
A read-only mount still permits FIFO I/O. Reading the live jobserver FIFO can remove a token.
Use a private `/dev` with approved devices such as `null`, `zero`, and `urandom`. Hide `/dev/steve` and its aliases.
Keep host jobserver descriptors and paths with the supervisor. Strip `CARGO_MAKEFLAGS`, `MAKEFLAGS`, and `MFLAGS`.
Reserve and return tokens in the existing supervisor. Pass only the admitted thread count to nextest.
Use private fixture FIFOs for jobserver tests. Verify those FIFOs, host endpoint refusal, and token return through the queue route.
[Linux FIFO permission check](https://github.com/torvalds/linux/blob/v6.18/fs/namei.c#L512-L520), [Jobserver token reservation](../../scripts/lint/invoke.sh#L64), [Nextest token use](../../scripts/lint/invoke.sh#L707)

### Processes

Use `--unshare-pid --proc /proc --die-with-parent`. Keep Bubblewrap as PID 1 inside the namespace.
Run the launcher as the gate's direct child. Exec Bubblewrap in that same process after cgroup registration.
Bubblewrap uses its parent-death option for the monitor, the reaper, and the command. The monitor passes the candidate status to the gate.
Recovery uses the recorded cgroup if a parent exits before Bubblewrap arms its parent-death signal.
Remove the host `MERGE_GATE_PARENT_PID` in the launcher. Set `MERGE_GATE_PARENT_PID=1` for the candidate.
`sitecustomize.py` uses the variable to skip startup folder allocation. The launcher supplies the lease.
`module_run.py` consumes the variable and watches the reaper's pidfd. It retains the parent-death signal for its own test child.
Skip `SUITE_BOOTSTRAP` on the guarded path. The trusted launcher applies its startup rules before candidate exec.
[Gate bootstrap](../../scripts/production/merge_gate.py#L121), [Python startup](../../scripts/lib/python_startup/sitecustomize.py#L220), [Module runner](../../scripts/test_times/module_run.py#L610), [Bubblewrap parent-death option](https://github.com/containers/bubblewrap/blob/v0.11.2/bubblewrap.c#L349-L374)

### Cgroup layout and cancellation

Use one phase 44df scope per admitted local gate under `builds.slice`, with `Delegate=yes`.
Keep `MemoryMax=44G` on `builds.slice` and keep the existing gate reservation. The gate scope has no separate memory limit.
Create one `keeper` leaf and a `job-<id>` leaf for each job. Move the keeper before enabling the memory and task controllers.
Keep the scope root free of processes. Set each job's `memory.max` and `pids.max` before attaching it.
Keep the sum of the job budgets within the reservation. Set each deadline in the trusted supervisor.
Publish the scope path in the reservation's cgroup sidecar. Record the scope invocation identity and each job leaf with its lease.
Record the wrapper's host PID and start time. Extend phase 383 recovery, which currently tracks process groups.
Keep the launcher waiting until the gate records its placement. Authorize Bubblewrap after the existing cancellation deferral ends.
If the setup channel closes before approval, exit before starting Bubblewrap.
If a gate cannot use its phase 44df scope, try existing admission with a verified job cgroup and the same controls.
A single-scope job can use its admitted scope with those limits. The survey requires its assigned scope.
[Queue scope](../../scripts/build_queue/queue_place.py#L88), [Phase 44df scope design](/session/queue-impl6/landcpu/scripts/build_queue/queue_place.py#L137)

The launcher inherits the gate's spawn mask. Exec trusted GNU `env --default-signal` with Bubblewrap's absolute path as its command.
GNU `env` clears the mask and restores default dispositions. Bubblewrap unblocks its internal `SIGCHLD` mask before candidate exec.
Add no signal handlers, masks, or disposition changes in new Python or shell code, including fixtures.
On cancellation, timeout, or failed setup, write `1` to the verified job's `cgroup.kill`.
Use the same stop if the monitor exits with live cgroup members. `SIGKILL` reaches descendants that use `setsid()`.
Reap the launcher. Require `cgroup.events` to show `populated 0` before release after normal exit, forced stop, or crash recovery.
Keep the cgroup available until the gate or recovery records that proof. Keep the lease if verification fails.
Require the same proof in the folder sweeper, even when a crash closes the lease lock.
Release gate admission after every job and the keeper have exited. Keep cgroup controls and the user manager outside the guard.
[GNU env implementation](https://github.com/coreutils/coreutils/blob/v9.10/src/env.c#L751-L754), [Bubblewrap exec](https://github.com/containers/bubblewrap/blob/v0.11.2/bubblewrap.c#L3370-L3392), [Gate recovery](../../scripts/production/merge_gate.py#L700)

### Outside access

Use `--unshare-net` with no external interface. Permit private Unix fixture sockets by default.
Allow IPv4 and IPv6 for the private loopback profile. Keep abstract Unix sockets in the private network namespace.
Hide the host system bus, user bus, credential agents, service sockets, session sockets, and network mounts.
A read-only mount can still expose a usable socket. Block other socket families, `io_uring`, and kernel keyring access.
Keep queue, logging, and result capabilities in the trusted supervisor. [Systemd execution controls](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html)

## Test requests and runner grants

The protected runner owns the policy outside the tested checkout. Read capability requests from static entries for a module or an exact test ID. Read the request before importing candidate code. The effective profile comes from the runner's reviewed policy for that repository and test ID.

| Request | Runner grant |
| --- | --- |
| No extra need | Private files, processes, and Unix fixture sockets. |
| `PrivateLoopbackFixture` | IPv4 and IPv6 within the isolated network namespace or guest. Create the server in the same sandbox. |
| `PrivateMountFixture` | A guest-owned mount fixture or a separately validated private namespace profile. Keep the same file, PID, and egress limits. |
| `PrivateServiceManagerFixture` | A disposable guest with its own real systemd and cgroups. Permit changes only to that manager. |
| `ScopedIntegrationRequest` | A dedicated fixture endpoint or controller operation for named actions and resources. Use a short expiry and dedicated credentials for that fixture. |

Use `GrantedTestIsolation` for approval. Use `TestIsolationUnavailable` with the failed setup operation for refusals. Only the runner can approve a request. Refuse unknown requests before loading the test. Keep fixture Git pushes inside a private bare repository. Use a fake message endpoint and a fixture AWS service for ordinary tests.

Use a trusted broker only when a reviewed integration needs an external operation. Grant one named operation and resource set, and record the decision. Exclude production accounts, session endpoints, Git targets, and service managers from test policy. Key reused gate results on the guard policy version and test grants as well as the tested tree. Keep all privileged publication in the separate controller.

## Mac — All three walls in a macOS guest

Use Apple Virtualization to run one disposable macOS guest per admitted job. The guest cannot see host processes or services.
[Apple Virtualization](https://developer.apple.com/documentation/virtualization/installing-macos-on-a-virtual-machine)

1. **Files.** Clone a sealed guest image into task scratch. Attach read-only source and a private writable disk. Use a private copy for source changes. Lease host scratch through `scratch.py` with an end.
2. **Processes.** Run tests under a guest identity. Keep boot, cancellation, guest shutdown, and disk cleanup with the Mac supervisor.
3. **Outside access.** Set `networkDevices` and `directorySharingDevices` to empty lists. Use guest loopback and private Unix sockets. Keep host credentials, agents, pasteboard, and command receivers outside the guest. [Virtual machine configuration](https://developer.apple.com/documentation/virtualization/vzvirtualmachineconfiguration)

Pass guest stdout, stderr, and console output through a bounded log channel. Render `mac_group=` and `mac_exit=` as log text.
Use a separate control channel. Give its descriptor only to the protected monitor in the sealed guest.
Bind the run identity to its VM handle in the host supervisor. Use that handle for cancellation.
Report exit status through the control channel. Treat a lost channel as a lost run. Only the host supervisor reports to the queue.
[Mac queue output](../../scripts/mac_test/mac_queue_run.py#L116), [Mac queue result](../../scripts/mac_test/mac_queue_run.py#L155)

Start the guest after trusted source staging. Preinstall the pinned compiler and dependencies. Let SwiftPM use its sandbox inside the guest.
Verify SwiftPM, Metal requirements, cold boot, and cancellation on the target Mac. Refuse jobs whose required feature the guest cannot provide.
The guest design remains unverified. See [Mac measurements](#mac-measurements) for the wrapper pilot.
[Mac offload](../../scripts/mac_test/offload.py), [Lint entry point](../../scripts/lint/invoke.sh#L31)

## AWS — All three walls on the execution box

Install the trusted Bubblewrap launcher and toolchain on the NixOS image. Give base and candidate jobs the same isolation.
Stage source as data with hooks and filters disabled. Fetch approved dependencies in the controller's credential scope.
Keep the controller's working directory and import paths outside the candidate checkout.
Start the guard before candidate Python startup, the `scripts.aws_target.gate_box` import, and `run_guest`.
Give the guarded process source, fixtures, private home and Python paths, and a bounded request channel.
[Guest bootstrap](../../scripts/aws_target/gate_box.py#L399)

Freeze the required module set from the trusted gate request. Validate each request against that set and the staged inventory.
Start each accepted module in its own sandbox. Keep artifact selection, upload, and result publication in trusted code.
Validate returned data for format and size. Record one terminal result per required module from trusted launch records and child statuses.
A refusal, timeout, lost result, or missing launch fails the gate. Require successful results for the complete set.
A retry can replace only its own result. Duplicate and unexpected results do not count.
[Module dispatch](../../scripts/aws_target/gate_box.py#L116), [Module results](../../scripts/aws_target/gate_box.py#L243)

1. **Files.** Bind source and tools read-only. Give each module private Git, temp, and build output storage. Keep cloud-init, signed URLs, signing material, and upload files outside the sandbox.
2. **Processes.** Give each job private PID and IPC namespaces and a fresh proc mount. Keep interruption monitoring and cgroup cleanup with the box supervisor.
3. **Outside access.** Hide external interfaces, VPC routes, DNS, IMDS, and local host services. Keep credentials and role access in trusted staging and upload. Use an image without an instance role when staging and retrieval can work without it. [EC2 metadata access](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/instance-metadata-limiting-access.html)

Expose a real GPU only under a reviewed grant for that device. Verify files, PIDs, loopback, IMDS, cancellation, and results on the image.
AWS launch and gate measurements are pending.

## Test start paths

Use the normal entry point for each verification. Certify only launches that the supervisor records as guarded.
An unrestricted host shell can start any interpreter. Enforce the protected profile in managed worker and manual test sessions.
Keep operator commands in separate entry points. Command aliases, Python startup hooks, and pre-tool hooks cannot enforce coverage by themselves.

| Start path on the base | Required change or named gap |
| --- | --- |
| Landing merge tests, checkpoint tests, fast-track tests, and base-red reruns | Route leaf tests from `checkpoint_gate.run_test_command()` to the guard. Let gate and verify supervisors start their own guarded test children. Keep fetch, commit, and push in the controller. [checkpoint_gate.py:117](../../scripts/production/checkpoint_gate.py#L117), [merge_checkpoint.py:1304](../../scripts/production/merge_checkpoint.py#L1304), [merge_gate.py:1086](../../scripts/production/merge_gate.py#L1086) |
| Full gate, tests-only, selected modules, retries, type check, and `targeted_test.py` | Apply the guard at `start_gate_job()` before every candidate `<repo>/scripts/lib/py` exec. Include `TYPE_CHECK`. `targeted_test.py` calls the same runner. Keep the boundary if phase 355 moves the runner into `gate.py`. [merge_gate.py:1086](../../scripts/production/merge_gate.py#L1086), [merge_gate.py:1722](../../scripts/production/merge_gate.py#L1722), [targeted_test.py:17](../../scripts/production/targeted_test.py#L17) |
| Nightly merge gate | Guard each module and type-check job in the nightly clone through `run_suites()`. Keep clone setup, notification, and slot sweep in the controller. [merge_gate.py:2563](../../scripts/production/merge_gate.py#L2563), [merge_gate.py:2730](../../scripts/production/merge_gate.py#L2730) |
| Queue-started test, lint wrapper, local `verify.sh`, and token-limited nextest | Select the guard before the test command reaches `buildlog_exec()` or the queue fallback. Keep the guard through the [step scope fallback](../../scripts/lint/invoke.sh#L165). Put nextest execution and its test subprocesses behind the guard. [queue_place.py:196](../../scripts/build_queue/queue_place.py#L196), [invoke.sh:774](../../scripts/lint/invoke.sh#L774), [verify.sh:3305](../../scripts/direct/verify.sh#L3305), [verify.sh:4815](../../scripts/direct/verify.sh#L4815) |
| Worker pane, manual session, and detached run | Provide a protected test shell for arbitrary commands. Connect worker and session test launches to it. Wrap detached test commands before the target shell starts. Detached accounting alone has no file rules. [worker_panes.py:1404](../../scripts/herdr/worker_panes.py#L1404), [pane_launch.py:1073](../../scripts/herdr/pane_launch.py#L1073), [detached_runs.py:1292](../../scripts/agents/detached_runs.py#L1292) |
| Raw `python -m unittest`, `pytest`, direct Python, `python -I`, or direct `module_run.py` | Start the interpreter inside the protected shell. The Python shim only makes a temporary folder for certain command shapes. It cannot enforce protection for another interpreter or before import side effects. [sitecustomize.py:220](../../scripts/lib/python_startup/sitecustomize.py#L220), [module_run.py:516](../../scripts/test_times/module_run.py#L516) |
| Raw `cargo test`, `cargo nextest run`, a test executable, shell tests, and hook checks | Use the protected shell for the entire command. Match the route by its execution boundary. A filename filter cannot find every test program. [test_agent_exec.sh](../../scripts/agents/test_agent_exec.sh), [test_cancel_prior.sh](../../scripts/validate_and_push/test_cancel_prior.sh), [BERTH hook checks](../../scripts/berth/install/README.md#L44) |
| `module_cost.py` time run and trace run | Guard both module starts from `measure()`, before `task_cpu.exec_stage()` executes the candidate module runner. Keep profiling and its lease lock in the trusted supervisor. Check both trace attribution and test results after integration. [module_cost.py:94](../../scripts/test_times/module_cost.py#L94), [task_cpu.py:217](../../scripts/test_times/task_cpu.py#L217) |
| `coverage_overlap.py` | Start its interpreter inside the guard. It imports and runs the suite in that interpreter. Keep `COVERAGE_OUT` inside the lease and collect its result in the supervisor. [coverage_overlap.py:219](../../scripts/test_times/coverage_overlap.py#L219), [coverage_overlap.py:328](../../scripts/test_times/coverage_overlap.py#L328) |
| `memory_page_proof.mjs` from the gate or a manual command | Guard Node before it starts Python for fixtures or starts Chrome. Inherit the test module's guard for calls from the gate. Use the protected shell for direct runs. Put the short Chrome profile, fixture output, and screenshots under the lease. [memory_page_proof.mjs:52](../../scripts/build_queue/memory_page_proof.mjs#L52), [memory_page_proof.mjs:158](../../scripts/build_queue/memory_page_proof.mjs#L158), [test_memory_feed.py:1131](../../scripts/build_queue/test_memory_feed.py#L1131), [test_memory_feed.py:3254](../../scripts/build_queue/test_memory_feed.py#L3254) |
| Mac offload, Mac queue, and manual Mac test sessions | Use the protected guest before starting the remote test. [offload.py:2024](../../scripts/mac_test/offload.py#L2024), [mac_queue_run.py:95](../../scripts/mac_test/mac_queue_run.py#L95) |
| AWS production and trial gates, and paired trials | Guard guest dispatch and the paired trial's direct module spawn on the execution box. [gate_box.py:116](../../scripts/aws_target/gate_box.py#L116), [gate_box.py:243](../../scripts/aws_target/gate_box.py#L243) |
| GPU trial archive preparation | Guard the controller-side build before candidate Nix shell preparation. Keep `nextest list --list-type binaries-only` and `nextest archive` inside it. Use private build and archive output paths. Export the verified archive through the trusted controller. Guard the later guest `nextest run` on its execution box too. [gpu_size_trial.py:647](../../scripts/aws_target/gpu_size_trial.py#L647), [gpu_size_trial.py:716](../../scripts/aws_target/gpu_size_trial.py#L716), [gpu_trial_wrapper.py](../../scripts/aws_target/gpu_trial_wrapper.py) |
| Hana AWS runner | Place the guard after source materialization and before `prepare_shell`. Keep export sourcing, `shellHook`, and `run_slot` inside the guard. Keep slot descriptors and build-start reporting authority in trusted code. [hana_runner.py:79](../../scripts/aws_target/hana_runner.py#L79), [linux_runner.py:220](../../scripts/mac_test/linux_runner.py#L220) |
| Linux SSH offload | Guard on the remote Linux host before `prepare_shell`. Keep the shell export, `shellHook`, and remote lint or test command in the guard. Keep the mirror lease, admission descriptors, and cleanup with the remote supervisor. [linux_runner.py:703](../../scripts/mac_test/linux_runner.py#L703), [linux_runner.py:770](../../scripts/mac_test/linux_runner.py#L770) |

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

## Report-only census and compatibility survey

Start after bob approves the plan and Builds confirms that natemerge allows tests. Follow the run order Builds sets.
Use normal admission and a detached run. Keep all three guards active. The report does not block landing during this stage.
Record the manifest at the tested SHA. At `81ac379ca` the inventory had 867 tracked Python modules and five tracked shell tests.
Use the gate's discovery and module runner. Add the following entries that the module inventory does not cover.

1. Run the five shell tests under the manual test profile. [Agent exec](../../scripts/agents/test_agent_exec.sh), [Models config](../../scripts/agents/test_models_config.sh), [Codex catalog](../../scripts/agents/test_sync_codex_catalog.sh), [Models registry](../../scripts/lib/test_models_registry.sh), [Cancel prior](../../scripts/validate_and_push/test_cancel_prior.sh)
2. Run the [BERTH hook check](../../scripts/berth/tests/check_installed_hooks.py) against a private installation.
3. Run `node scripts/build_queue/memory_page_proof.mjs --gate` with real Chrome and private output. Mark it incomplete if Chrome is unavailable.
4. Check guarded type checking, both `module_cost.py` stages, and the `coverage_overlap.py` interpreter. Record other commands found at actual entry points.

Keep the gate module that tests real and fixture Chrome in the manifest.
Repair `MergeGateFixture.setUpClass()` before the census. Replace its absolute `/tmp` allocation with the verified private parent.
Pin the fixture's `roots.gate` before selecting its tmpfs parent. Apply the repair to callers and keep short socket paths inside the lease.
[Merge gate fixture](../../scripts/production/merge_gate_test_support.py#L319), [Parent selection](../../scripts/lib/build_folders.py#L197)

### Write and lookup trace

Trace the job and its descendants. Decode directory descriptors and record the syscall, path, PID, and result.
Include creates, writes, truncation, metadata changes, deletion, and both paths of rename and link operations.
Record lookups, scans, and writes at live roots and aliases. Include failures before lookup and live path names recreated inside private mounts.
Count attempts even when the test passes or skips. Classify deliberate canaries separately.
Separate fixture setup, test cases, and teardown. Attribute by module first. Rerun with case names and times when necessary.
Record guard refusal, import failure, and class setup failure separately. Mark tests blocked by setup as unobserved and rerun after repair.
Bound raw output per module. Keep a compact summary and remove the raw output before the next run.
Record module status and counts for guarded runs, setup refusals, violations, and missing results. Report any lost data or interruption as incomplete.
Require a result for every entry before reporting an empty violation list. Give legitimate writes private fixtures.

### Compatibility survey runner

Use the existing [survey runner](/session/natest-impl5/guard-walls/probe_walls.py) outside the candidate checkout.
The survey stages an independent checkout of the exact base. It runs one module at a time with private loopback.
Compare its test counts, errors, and skips with the recorded base. Repeat new errors under the guard and record the required resource or syscall.
Classify modules that require the private loopback grant. Treat any new skip as a loss of coverage.

The runner reserves a merge-gate place and verifies the assigned scope and memory grant under `builds.slice`.
It uses 512 tasks, an eight-hour survey deadline, a 15-minute module deadline, 1 GiB of temp space, and 64 MiB of shared memory per module.
The supervisor checks the count record from a separate pipe against the child status.
Exit 5 counts as empty only if the record gives zero tests, failures, errors, and skips. Missing or invalid counts fail the module.
Drain logs in chunks of up to 8 KiB. Retain up to 8 KiB from each end of a module's log.
Stop retaining bytes at 16 MiB. Keep draining until exit or timeout. Record the offset and retained and omitted bytes in each JSONL row.

### Gate proof and enforcement

| Case | Required evidence through `start_gate_job()` |
| --- | --- |
| Normal module and type check | The first shim can write only inside the lease. The supervisor receives the command status. |
| Python startup and module runner | Startup uses the supplied lease. The module runner watches namespace PID 1. No host PID reaches either reader. |
| Gate exits before scope registration | Closing the setup channel stops the launcher before Bubblewrap starts. |
| Gate or monitor exits during setup | Recovery stops any remaining members before releasing the lease. |
| Cancellation or timeout with a `setsid()` descendant | Reap the launcher and verify that PID 1 and the descendant exited before releasing the lease. |
| Startup after the existing deferred spawn | Read the signal state at first exec. `SIGTERM`, `SIGHUP`, and `SIGINT` have default dispositions and the mask is empty. |

Use disposable canaries to check wrong-root unlink and Git commit through every start path.
Verify that Bash skips a disposable script supplied through `BASH_ENV` on the type-check route.
Check mount flags with `findmnt -T` and inspect `/proc/self/status`.
Verify that inherited writable descriptors close, that both bind aliases reject mutations, and that host `/proc/<pid>/root` is hidden.
Require the guard after fixture repairs and enforcement of the worker and manual session profile.

## Measurement evidence

### Natedev file prototype

The earlier file probes used UID 1000, Linux 6.18.50, util-linux 2.42.2, and systemd 260.2 on 2026-11-19.
The [session transcript](/home/natepiano/.codex/sessions/2026/11/19/rollout-2026-11-19T08-36-04-01a1f405-844f-7aa2-b750-7ecf8791d2f5.jsonl) has the commands and results.

| Transcript lines | Saved output |
| --- | --- |
| 495 and 524 | Mount sources, scope option refusal, versions, and plain mount namespace refusal. |
| 551 | Root-map flags, write refusals, and private writes under the `/session` bind. |
| 586 and 589 | Root-map, scope, user service, and Landlock timings with Git canaries. |
| 696 and 699 | Mapped-user timings, file canaries, proc mount, and direct module run. |

| Earlier file prototype | Median | Range | Result |
| --- | ---: | ---: | --- |
| Direct start in mapped-user batch | 3.64 ms | 2.65–5.69 ms | Baseline. |
| Plain `unshare -m` | — | — | `EPERM`. |
| `unshare -rm` with recursive read-only mount | 27.22 ms | 22.30–38.74 ms | Maps UID 1000 to UID 0 without host root. |
| Mapped-user mount with private proc | 18.25 ms | 15.62–64.18 ms | Keeps UID 1000. |
| `systemd-run --user --scope` | 16.28 ms | 14.11–73.03 ms | The scope refuses `ReadOnlyPaths`. |
| `systemd-run --user --pipe` service | 57.82 ms | 45.66–106.60 ms | Applies `ProtectSystem=strict`, `ReadWritePaths`, and `ReadOnlyPaths`. |
| Landlock ABI 7 | 47.28 ms | 30.01–232.05 ms | Blocks append, truncate, and unlink with `EACCES`. Permits `chmod`. |

The timing rows use seven sequential starts of `true`. The commands saved counts, medians, and ranges.
The root-map and user service probes blocked a Git fixture commit at `index.lock` with `EROFS`.
The root-map run of `test_process_failure.py` reached a five-second timeout. Its result under the production guard is pending.
The mapped-user probe blocked append, truncate, chmod, dirfd unlink, and writes through a symlink alias with `EROFS`.
Rename across the bind returned `EXDEV`. The probe opened the directory descriptor inside the guard.
The host reported `/session` writable. Inside the guard, `/`, `/session`, `/build`, and the checkout were read-only.
Private writes succeeded. The probe ran one test from `scripts/lib/test_git_index.py` through `module_run.py` with private `TMPDIR`.

### Natedev three-wall pilot

The pilot used non-setuid Bubblewrap 0.11.2. Each row measures 15 sequential starts of `true` through exit, with no artificial load.
The earlier `unshare` probe created PID and network namespaces. Systemd 260.2 supported `PrivatePIDs=yes` and `PrivateNetwork=yes` in user services.

| Natedev launch | Median | Range |
| --- | ---: | ---: |
| Direct | 5.013 ms | 2.043–10.924 ms |
| Systemd user service | 61.229 ms | 36.271–126.514 ms |
| Systemd service with PID wall | 70.504 ms | 39.802–131.603 ms |
| Systemd service with network wall | 73.819 ms | 50.743–134.123 ms |
| Systemd service with both walls | 90.409 ms | 53.888–166.000 ms |
| Bubblewrap with all walls and seccomp | 40.309 ms | 13.493–265.202 ms |

The timing predates the synthetic passwd and group files. The final pilot used them in run `20261119T132023-1344039-a94e`.
The guarded process kept UID 1000 with no effective capabilities. It saw a fresh proc mount and only the loopback interface.
Signal 0 to an owned host PID returned `ESRCH`. Connections to host TCP, pathname Unix, and abstract Unix listeners failed.
Private TCP and Unix listeners accepted connections. The root and checkout were read-only. The private temp folder was writable.

| Existing test | Guarded result | Required action |
| --- | --- | --- |
| `herdr/test_pane_columns.py` | 2 passed through `module_run.py` in a private network namespace. Its fake Herdr uses a private Unix socket. | Use the default profile. |
| `usage/test_collect_processes.py` | 2 passed through `module_run.py` with private loopback. `test_running_pass_keeps_its_lock_through_the_mac_read_until_exit` failed with `EAFNOSUPPORT` under the Unix-only profile. | Declare a private loopback fixture. |
| `build_queue/test_queue_place.py::QueuePlaceTests.test_nested_scope_stops_an_unsampled_setsid_descendant_before_release` | 1 skipped because the minimal root had no cgroup v2 mount. The fixture also requires a real user manager. | Run this kernel check against a guest-owned manager in a disposable Linux guest. |
| `lib/test_session_move.py::SessionMoveTests.test_hold_through_a_bind_mount_keeps_the_source_tree` | 1 failed with `ENOSPC` when the guard disabled new user namespaces. The same row passed when it permitted a nested user namespace. | Use a reviewed private mount fixture profile or the disposable Linux guest. |

Of 867 modules, 34 directly reference `systemd-run`. Some use a stub. The real AWS CLI test needs private loopback.
[Admission fixture](../../scripts/build_queue/admission_test_support.py#L711), [Mount fixture](../../scripts/lib/test_session_move.py#L267), [AWS CLI fixture](../../scripts/aws_target/test_real_aws_cli.py)

### Mac measurements

The arm64 Mac used macOS 27.0.1 build 26A434. `kern.hv_support` returned 1.
The pilot measured seven sequential starts of `true` per row.

| Mac launch | Median |
| --- | ---: |
| Direct | 5.973 ms |
| `sandbox-exec` with an allow profile | 23.685 ms |
| `sandbox-exec` with network denied | 21.325 ms |

Both sandbox profiles refused `ps` with exit 71 and let Python check the same-user host parent with signal 0.
The allow profile let a fixture bind loopback. The denied profile returned `EPERM` for that bind.

### Pending evidence

Record individual samples and raw proof output after authorization. The timing probes saved only counts, medians, and ranges.
Phase cleanup removed the detached three-wall pilot log after recording its results.
Save the next results under [the evidence folder](/session/natest-impl5/guard-walls/evidence).

| Measurement | File |
| --- | --- |
| Cost for each Bubblewrap wall and the integrated gate | `natedev-bubblewrap-walls.tsv` |
| Mac guest cold boot, module dispatch, and cancellation | `mac-guest.tsv` |
| AWS launch and module cost on the NixOS image | `aws-guard.tsv` |
| Guarded survey counts and exit statuses | `survey-results.jsonl` |
| Bounded survey log | `survey-output.log` |
| Wrong-root trace summary | `wrong-root-census.jsonl` |

## Code phases and ETA

Estimates use eight worker hours per workday and start after bob approves the plan. Count shared launcher and survey work once.
Queue time and host availability can extend these estimates. Update the fixture estimate after the census.

| Code phase | Worker hours | ETA and completion evidence |
| --- | ---: | --- |
| Linux launcher, grants, admission, and lease recovery | 12–18 | About two workdays. Verify all three walls before the first candidate exec. |
| Local start paths and explicit delete roots | 8–12 | One to two workdays. Connect gate, queue, and manual routes. Coordinate sweep ownership with Queue. |
| Census, compatibility survey, and fixture changes | 8–20 | One to three workdays after natemerge releases the hold. Report results for the full manifest. |
| Mac guest image, runner, and admission | 12–18 | About two workdays. Verify cold boot, SwiftPM, Metal, and cancellation. |
| AWS image, guest bootstrap, GPU archive, Hana, and Linux SSH | 8–12 | One to two workdays. Verify fixture requests and trusted results. |
| Verify routes, measure cost, and require the guard | 5–8 | About one workday after fixture and session repairs. Record proof for each route. |
| **Total** | **53–88** | **About 7–11 workdays in sequence.** |

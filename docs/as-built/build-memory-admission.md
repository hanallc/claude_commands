# Build memory admission: builds that fit in memory

## What it is

Build admission keeps CI and session builds within available memory on natedev.
The target is a normal working day without an earlyoom kill.
On 2026-10-04, 58 earlyoom SIGTERMs failed build steps and CI jobs.
Two admission systems previously could not see each other.
CI ran outside steve in a slice whose limit counted page cache.
Session compilers grew after steve admitted their slots.
Full swap caused earlyoom's swap condition to remain true.
A build hold released every session at once.
Session builds and compilers now share one bounded user slice.
CI uses a smaller slice and slot pool, with a lower kill priority than sessions.
Build steps sit above both, so earlyoom kills the largest build first.
Every compiling step and BRP launch waits for free memory.
A build-hold release sends held sessions through those memory gates.
The build log records slice samples, snapshots, memory reports, waits, kills, and CI freshness.
This work also hardened the example-test guard, Cargo token recovery, CI queue accounting, BRP records, disk alerts, Codex capacity handling, and session broadcasts.

## How it works

`lint/invoke.sh` exports `BUILDLOG_RESERVED_STEP` only into a reserved command.
The marker contains the reservation ID, its holder PID, and the queue state folder.
`admission_client.py` checks the marker against the current queue and its durable `state.json` before a new admission.
It refuses a nested verify step while the same reservation and holder (pid and start time) remain live.
The memory and cargo-token entry points use the same check before waiting.
Markers for other queues, dead holders, reused pids, and released reservations do not block a step.

### Installed writers and private test state

Only the installed scripts under `~/.claude/scripts` may change shared build state.
Checkout scripts may change private test directories.
`override_guard.shared_state_access()` checks the actual paths, including paths reached through symbolic links.
`SharedStateWriteRefused` stops an imported mutation before it writes shared state.
The CLI prints the refusal and returns status 2.
The installed scripts retain their environment override refusal.

`AdmissionServer` validates its state directory before restoration.
`ServiceStateWrites.register()` checks saved paths during restoration and new paths when a reservation enters state.
These checks include maximum, wave, history, lifecycle, and sidecar paths.
Later requests and passes reuse this validation.
`StateIntents` uses this same boundary.
`export_priority()` checks its directory and saved reservation paths separately.
Cancellation recovery retains its saved time and queue identity.
The client still obtains its initial estimate through `step_estimate()`.
The estimate cache has no writer.

`release_lock()` validates both hold directories before it creates the lock.
It yields `ReleaseLockHeld`, bound to those directories and the open lock.
Receipt migration and damaged record recovery require this capability.
The helpers do not acquire another lock.
`cycle_path()` and `read_cycle()` return `NoCycle` when no current cycle exists.
Hold status reads records without a lock or a mutation capability.
The hold stop guards the real process root and hold, release, and enforcer directories before any signal or record write.
The stop still targets the command group and preserves supervisor result delivery.

`override_guard.helper_root()` and `build_helper_root()` select the helpers for Python and shell callers.
A caller with any `OVERRIDE_VARIABLES` name set uses the helpers beside its own file.
An empty override value also selects those helpers.
Other callers use `$HOME/.claude/scripts`.
This rule covers admission, hold, target, release wait, detached run, recipient registration, and unit retirement helpers.
Test fixtures create no helper links in the real home.
`merge_gate.py` already runs from the installed copy; its hold reader remains unchanged.

Service waiters retain `repository_name` separately from `step`.
Reservations retain `repo` separately from `step`.
Queue queries use these recorded fields without a repository prefix parser or a required live `.waiting` file.
This rule changes no Builds page layout.
The guard obtains each shared root from its owner's default.

The admission service keeps `/proc/vmstat` `pswpin` samples in memory.
`agent_swap_window_seconds=600` sets the sample window.
The window total is the newest page count minus the oldest page count inside the window, multiplied by the page size.
The first sample establishes the baseline.
The reserve cannot exceed current swap outside builds and CI.
`agent_swap_share_percent` applies to that amount.
Idle swap with no swap-in needs no reserve.
An unreadable or missing `pswpin` reading returns `ReadingUnavailable` and stops new admissions.
The service publishes `HostMemory.swap_in_window_bytes` in its existing queue snapshot.
The feed, evidence text, and CLI use that snapshot and the shared `memory_room` calculation.
The page keeps `agent_swap_demand` and `swap_covered_bytes` as its reserve fields.
Each new host reading still reads `MemAvailable`.
The pressure limits remain unchanged.

The service measures each handled request's wall time and process CPU time.
The measurements include configuration recovery for the logging threshold.
`request_slow_seconds=0.25` sets the logging threshold in `config/build_admission.conf`.
A request above that threshold logs its request kind, wall seconds, and CPU seconds to stderr.
The service reads the threshold at use.
An unusable threshold logs once through the existing configuration diagnostics.
Requests and cleanup continue when this logging setting is unusable.

### Build filesystem space

The natedev service reads `free_floor_gib` from the `[disk]` section of `build_admission.conf` each pass.
The service measures available space at the build root (`build_folders.conf` `roots.build`, `/build` on natedev) once per pass.
If space is below the floor or the reading fails, new local steps wait in order.
Running steps continue.
Eligible remote steps can start on a remote target.
The Builds page and the queue head alert show the measured GiB and the floor or the read error.
Older clients read the disk wait as a wait behind the build space floor.
Waiters can refresh expired estimates during the hold.
The next pass can start local steps when a successful reading reaches the floor.
A missing floor leaves the space check disabled.
If a reading fails, the service logs the error.

### Placement status

`PlacementStatusRequest(episode, attempt)` reads the saved assignment without changing state or launch permission.
`PlacementStatusReply` contains a `KnownPlacementStatus` or an `UnknownPlacementStatus`.
A known status includes the assignment, its current or closed state, and its provider claim reference or `NoProviderClaim`.
The assignment identifies the destination target, owner, and session.
A closed state includes its reason.
An unknown status means the service has no saved assignment for that episode and attempt.

Run `admission_client.py placement-status --episode E --attempt A` for one tab-separated line.
The fields are state, target, owner, session, claim ID, and reason.
The owner uses `host:pid:start_time`.
Absent fields use `-`.
The command returns 0 for current, 1 for closed or unknown, and 2 for an unreachable service.
A live service that answers slowly makes it wait, as the placement requests do. A missing socket or a refused connection returns 2 at once.
A service that stays silent until `unreachable_limit_seconds` returns 2 with "build queue unreachable for <time>".
A query holds no step and no cargo token, so its reason does not say "step stopped; cargo token given back".
`_query` does not print a refusal. The caller prints the reason once.
The Mac cargo gate runs this check over ssh. A failed check refuses the call with the status and the last error line of the check.
The check grants no permission and records no caller version.
The status and replay paths share the same placement lifetime check.
An ended local reservation or run returns closed even when its saved assignment still says current.
Without a terminal report, claim release requires a later provider reading, an ended holder, and no pending start.
A pending dispatch report keeps the placement current even after an empty provider reading.
A Mac claim whose holder and submitting owner ended frees its placement at once, even when the Mac keeps the claim.
The next claim for the scope takes the kept claim over and checks for leftovers.
A pending start keeps the placement current.
The service watches each claim holder, so its end wakes the service loop.
A settled decline requires provider evidence at or after the report time.
The status check uses cached provider evidence and starts no probe.
Status and settlement apply the same evidence expiry check.
Both paths compute the evidence key from the submitted request and current configuration.
Settlement schedules the probe before it reads the shared evidence result.
Local placements release their reader subscriptions when their reservations end.

`RepositorySlotReady()` advises a retry because `/proc/locks` can omit a held slot, and only the caller's successful `flock` proves it acquired the slot.

### Cloud gates

`AdmitRequest.place` defaults to `LocalPlace()`.
This tree supplies the queue API and the cloud progress file.
The aws route integration belongs to a separate tree.
That route calls this API with `CloudPlace("aws")` for a `gate` or `test-affected` step.
The route process owns the reservation.
The service releases the cloud slot when that process ends.
An explicit release also ends the reservation.
A route that returns to natedev releases its cloud reservation before it requests local admission.

Cloud reservations need zero local memory.
They do not consume memory room, CI room, head protection, start spacing, or progress memory.
Local waits and cloud waits have separate admission orders.
Cloud gates wait in arrival order when all cloud slots are in use.
The wait text includes the used slots and the slot limit.
`cloud_gate_slots=9` sets the limit in `config/build_admission.conf`.
The service reads the limit through its configuration check before each pass.
The pass uses the checked values if the file changes during that pass.
The next check handles an unusable cloud setting like other unusable settings.

`cloud_progress.publish(sidecar, progress)` writes `<sidecar>.cloud.json` with the shared atomic writer.
`CloudProgress(state, size_vcpus, failed_modules, updated_at)` stores the cloud state.
`CloudState` names `requesting`, `setup`, `running`, and `uploading`.
`cloud_progress.read(sidecar)` returns `CloudProgress` or `NoCloudProgress`.
`NoCloudProgress.reason` distinguishes missing data from unreadable data.
The API supplies `progress_profile.publish_counter` for the route to write module counts.
The service removes the progress files when the reservation ends.

The Builds page shows each cloud gate in the main table.
A running row shows machine `aws` and its vCPU count.
On the build page, a cloud row with a module counter draws the local rows' progress line.
Small text under the line shows the module count and failed modules.
The page omits the state `running` and shows any other state first.
A waiting row shows `cloud` in the # column and its slot wait text.
Its machine and vCPU cells stay blank.
When no step waits, the build page's queue summary hover shows `AWS: N of M slots in use`.
Missing progress and module counts have explicit display states.
`cloud_gate_stale_pushes=3` sets the update age threshold in `config/build_admission.conf`.
The queue reading carries the checked threshold to the display.
The display reads the push interval from `conf.read("cloud_gate").number("log_push_s")`.
An older update shows `no update for <age>` in red.
The Builds page uses one age formatter for its rows.
`CloudPushIntervalUnavailable` names an unavailable push interval.
The display omits the stale age when the push interval is unavailable.

The wire envelope uses protocol version 3 and keeps the existing admission code version rule.
The service accepts client versions 1, 2 and 3.
The service sends a version 1 refusal when it cannot determine the request version.
All three client versions can decode that refusal.
Version 1 view and mine replies carry plain `QueueRead` records without cloud rows.
Version 2 replies can carry `CloudQueueRead` records.
The shared codec encodes the new place field and its frozen dataclasses.
Old requests default to local admission when they omit the place field.

Version 3 supplies placement messages for converted verify, offload, copy-watch, and shared lint callers.
`PlacementRequest` carries the episode, profile, fixed constraints, checked coordinator, run identity, and local admission context.
The context carries the actual arguments, sidecar, call identity, test width, and fresh-test state.
Missing context becomes `LocalAdmissionUnavailable` at the wire boundary.
Configured candidates remain in the request through disk, busy, hold, and reachability changes.
Fixed pins, platform rules, tree writes, local artifacts, and final-step rules restrict those candidates.
Dynamic exclusions remain refreshed evidence.
Saved disk exclusions remain diagnostic evidence.
`SourceKeyKnown` carries an available source key.
`SourceKeyUnavailable` preserves the existing copy path when neither source key is available.
An unavailable key permits placement and sends no source key with the copy.
`PlacementWait` carries the original queue time, position and candidate eligibility.
`Assignment` carries a persisted destination and attempt; it grants no execution permission.
`ClaimFailure` closes the exact assignment before another placement decision.
`ConsumePermission` carries the exact assignment, adopted provider claim reference, and checked claim-holder identity.
The coordinator remains separate from its offload child.
The service checks the local process tree before it accepts that child as the holder.
When the last system reading does not show the holder, the service reads the local processes again for this check.
It does the same when the reading shows the holder pid with another start time.
This read of the full process table costs about 16 to 19 ms with about 1300 processes, at most once for each consume, when the last reading does not show the holder with its start time.
For a `mac-linux` assignment, the guest probe reports no claims.
The service accepts the consume when the holder holds the named claim in the local Mac claim state (`mac_run.claim_held`).
Cleanup takeover supplies no launch authority.
The first valid consumption returns `FirstAuthorization` after the state write completes.
Every repeated consumption returns `DispatchStatus`, including a retry with a different transport request id.
Unknown, changed and closed attempts grant no authority.
`ExecutionReport` supplies typed outcomes and execution evidence without creating another durable provider lifecycle.
`DispatchPathSettled` confirms that the adapter has resolved its pending claim and start operations.
An empty remote observation cannot establish that condition.
A settled prelaunch refusal permits retry only after fresh provider evidence confirms that the exact claim has departed.
The service retains terminal reports while cached evidence still contains the claim.
Later passes reevaluate those reports after provider evidence refreshes.
An exact settled Hana journal can confirm a prelaunch refusal with no command start.
The first pass after service replacement can use this journal to close a saved refusal.
The step can then use another eligible target in the same episode.
A pending Hana refusal with unused permission returns the same assignment so the adapter can finish settlement.
A repeated request for an assignment with consumed permission returns a wait.
The saved terminal report preserves the outcome, dispatch settlement, and original report time across restart.
Execution observations remain reconstructed evidence.
The retry deadline uses the original report time.
`AttemptDeclined` preserves caller policy for no-tests results and lost runs after confirmed cleanup.
An unavailable observation cannot substitute for confirmed cleanup or a settled dispatch path.
Busy, blocked, and disk refusals retain normal polling.
A Mac that `free_now` reports busy is a busy exclusion in placement evidence, even though routing keeps it eligible.
The step then waits for the Mac, or takes natedev when natedev is a candidate, and no attempt starts until the Mac is free.
Before this rule, a waiting Mac step made one refused attempt each second.
A kept Mac claim is not busy, because the next claim takes it over.
Before this, a dead copy's kept claim held the Mac with no work until the claim watcher released it after 15 idle minutes.
The service also freed the dead copy's placement only when the claim watcher released the claim. On 2026-11-14 this held the Mac with no hana step for 16 minutes.
Other temporary Mac refusals delay that destination for 180 seconds.
A current `mac-linux` assignment of another episode makes the guest busy.
The guest gate consumes its launch permission before the guest run starts, so its assignment stays current while the gate runs.
A guest gate that took no run sends its claim failure after the gate process ends, and the queue closes the attempt with the guest delay.
While `copies.py` lives, a pass that reads the ended gate does not close the attempt.
Only `copies.py` knows if the gate took a run, so its claim failure or its end closes the attempt.
So the claim failure finds the attempt current, and the queue places the episode only once.
When the gate ran and ended, the attempt stays current until `copies.py` ends. Then the next pass closes it as lost with `RetryAt(now)`.
Final command results remain terminal.
A retry retains the episode, queue time, run place, and priority.
Each retry receives a new attempt identity.
A late report cannot close a replacement attempt.
Version 1 and 2 requests retain their existing local or scaler destinations and reply shapes.
Placement messages require version 3.

Converted local candidates and fixed local waiters use the same queue order and memory planner.
Priority changes affect the next placement pass.
The planner evaluates continuation memory, pressure, spacing, starts-alone admission, and head protection.
Remote selection removes the local projection before local effects commit.
It releases that waiting step's unused continuation claim.
It changes no local start spacing.
`LocalPlacementGranted` carries the existing local reservation and execution controls.
The adapter uses that grant without a second admission request.

`TokenRequired`, `TokenHeld`, and `TokenNotRequired` describe the local execution prerequisite.
`PrerequisiteReport` updates that state separately from fixed constraints.
Restart requires fresh token confirmation for requests that need a token.
A placement waiter holds no cargo token.
A step that waits for placement to choose a Mac Linux guest has the reason `MachineChoiceWait`.
Only a queue row carries it, and the Builds and Limits pages show it as `waits for a machine choice`.
`_placement_row` marks every `TokenRequired` row token-waiting, so `memory_admit.py` does not admit it.
`verify.sh` sends `--token after-grant`: it takes the token after a local grant.
Its first pass sends `TokenRequired(TokenReadinessUnchecked)`.
That pass starts nothing on natedev, so a free remote target can take the step.
The client shows no memory wait for that pass, and `verify.sh` sends the next pass at once.
Later passes send `TokenNotRequired`, because the service needs no token before that grant.
These are existing wire values, so a new `verify.sh` works with a service that started before this change.
An older caller sends `--token required` (`TokenUnavailable`) and gets no local grant until it reports `TokenHeld`.

Remote rows carry execution evidence and the assigned host.
Physical grouping includes the host, generation, PID, and start time.
Unavailable remote identity remains uncertain.
Remote memory remains unavailable and contributes zero to local admission accounting.
A Mac host run shows on the Builds page as its Mac build row; the page hides its queue row, so the run shows once.
A `mac-linux` gate row has machine `mac-linux` after the gate consumes its launch permission.
The service sets the remote `started_at` from the consume time, so `memory_feed.unconfirmed_guest_run` reads the consume from it.
A confirmed guest attempt gives the row `guest_run`. A consumed attempt that the queue has not confirmed gives `guest_unconfirmed: true`.
The page (`guestRow` in `memory_live.html`) shows both in the running table.
The Machine cell of a guest row shows the bright `mac-linux` icon. The hover of an unconfirmed row shows `Guest attempt  not confirmed`.
A speculative local parent retains its actual reservation.
The queue selects the newest eligible speculative copy by call start time, then call identity.
The watcher keeps result collection and loser stopping.
`CloudSlotWait` carries the used slots and slot limit in wait replies.
`CloudQueueRead` extends the local queue reading with cloud rows and the slot limit.
Local queue readings keep their existing wire form when no cloud row exists.

Linux target steps set `BUILDLOG_CALLER=verify-linux`.
`memory_gate.sh` grants these steps without a local admission socket or a natedev hold check.
Target slots and the target build budget provide admission there.
AWS admission still requires the spending guard.
Other Linux callers keep the local admission and hold checks.

### Temporary build folders

`scripts.lib.build_folders.create_build_folder(request) -> Path` allocates temporary build folders.

1. `UniqueBuildFolder(prefix, parent)` creates a private folder atomically with mode `0700`.
2. `StableOwnerFolder(name, parent, mode)` retains the owner's folder, contents, and existing permissions.

`FOLDER_PREFIXES` defines each build kind's prefix.
Dynamic prefixes use an `owner` field for the process, module, review, version, or cache key.
`MKDTEMP_SUFFIX_PATTERN` defines the allocator's eight-character suffix rule for cleanup readers.
The sweep also reads old six-character shell suffixes until 2026-12-03.

`InheritedTemporaryParent` reads `TMPDIR` at each allocation.
`inherited_temporary_parent()` exposes that same rule to callers that retain their own folder creation.
`ExplicitBuildParent` retains an explicit parent.
`SystemTemporaryParent` uses the short system parent for socket and index folders.
`UnitRunParent` reads the current root from `unit_runs.run_roots()`.
The merge gate checks `/dev/shm` before allocation and uses an explicit parent without a disk fallback.

Shell callers use these forms:

1. `unique --kind KIND [--owner TEXT] [--parent PATH | --system-temp]`
2. `unique --prefix TEXT [--parent PATH | --system-temp]`
3. `stable --name NAME (--parent PATH | --unit-root)`
4. `stable --path PATH`
5. `stable --run-temp RUN`
6. `run-temp-path RUN`

The CLI prints one path on success.
Shell callers request allocation only when a stable folder is missing.
`verify.sh` first sources `invoke.sh --prepare-temp` to create a missing inherited `TMPDIR` before environment loading.
This mode uses the same allocation block as a normal `invoke.sh` call.
`unit_runs.RUN_TEMP_NAME` defines the run temporary folder name.
`staff.set_run()` stores the open run path in the director's staff row.

`temporary_build_folder()` yields a `Path` and uses the standard library's cleanup and permission repair.
Other callers retain their cleanup traps, owner records, task records, and release order.
The three shell verify allocations retain the CLI cost required by the Work Order.
The promotion check sent over ssh uses a self-contained standard-library temporary folder.
It imports no repository modules before the Mac fetch.

The director removed the unused task and scratch-parent wrappers during repair round 1.
This decision departs from Spec items 6 and 8.
`scratch.new()` and `scratch.target()` retain task registration and task naming.
`copy_record.root()` and `copy_record.copies_root()` retain the scratch and copies root rules.
The sweep reads those authorities and `unit_runs.run_roots()`.
An explicit private sweep root selects its own `copies` folder instead of the live copies root.

Stable session folders retain their short paths and mode `0700`.
The session hook defines the session temporary root.
The admission fixture uses `short_socket_directory(prefix)` for its separate short socket parent.
The service keeps `admission/state.json` and `admission/queue.json` outside temporary build folders.
`step_estimator.is_fresh_test()` and the disk sweep read `tmp_sweep.state_dir()` at use.
`tmp_sweep.TARGET_DELETES_NAME` defines the shared deletion record name.

### Machine side (`/etc/nixos`, natedev only)

Not in this repo; listed because the code here reads it.

| Piece | Setting |
| --- | --- |
| `builds.slice` (user slice, `modules/linux/development.nix`) | `MemoryMax=44G`, no `MemoryHigh`, `MemorySwapMax=4G`, `CPUWeight=20` against `app.slice`'s 100, so the sessions and their panes get the CPU first. CI's `hana.slice` sits at 5 against `user.slice`'s 100 (`modules/linux/hana-runners.nix`). Holds every recorded step's scope (`run-*.scope`), the `sccache.service` user service, and the `buildlog` unit (nightly Rust trial, `OOMScoreAdjust=200`). |
| `builds-detached.slice` | Holds detached commands under `builds.slice`, whose limits still apply. Detached Python memory appears in Other without a queue row. A direct cargo call takes the hold check and enters a new scope in the run's own slice. A cargo call in a queued step starts at once. Its memory counts once as build memory through the step's reservation. |
| `sccache.service` | Runs the server in the foreground with `SCCACHE_NO_DAEMON=1` and `SCCACHE_START_SERVER=1` on `ExecStart` only; takes the port from an already running server on start; `RestartSec=5`. Every compile handed to sccache therefore runs inside `builds.slice`. `OOMScoreAdjust=500`, so its compiles rank with the build steps. |
| `hana-ci.slice` | `MemoryMax=18G` only, `MemorySwapMax=2G`, no `MemoryHigh`. Runners `OOMPolicy=continue`. |
| CI slot pool | `/dev/steve-ci`, 14 slots, separate from the sessions' `/dev/steve` (31 slots). Runners: `CARGO_MAKEFLAGS=--jobserver-auth=fifo:/dev/steve-ci`, `BindPaths=-/dev/steve-ci`, `DeviceAllow` `char-rtc r` and `/dev/steve-ci rw`, ordered after `steve-ci.service`. No `CARGO_BUILD_JOBS`. |
| Build and CI kill order | Build steps and sccache at 500: each step's `builds.slice` scope sets `oom_score_adj=500` before starting the step, and `sccache.service` has `OOMScoreAdjust=500`. Session processes at 200 (rust-analyzer, builds outside `invoke.sh`). CI jobs at 100: `PIPELINE_JOB_OOMSCOREADJ=100` in the runners' `extraEnvironment`. earlyoom kills build steps first, then sessions' processes, then CI, the largest first within each level; the processes `--avoid` names go last. |
| Swap and earlyoom (`modules/linux/memory.nix`) | zram (zstd, 50%, priority 100), `vm.swappiness=100`. earlyoom decides on available RAM alone (SIGTERM at 5%, free-swap thresholds 100). Its score is roughly usage‰ of RAM plus swap, plus oom_score_adj, so the kill order comes from oom_score_adj. No `--prefer`; `--avoid` takes 300 from remote access, the desktop and the sessions themselves (`sshd`, `tailscaled`, kwin, plasmashell, `claude`, the tmux server and `herdr` among them). |
| Timers | `buildlog sample` every 60 s, `buildlog disk` every 600 s, `buildlog hourly` every 3600 s, disk-floor every 2 min (`sweep.py --floor-only`). |

The two ceilings (44G + 18G) deliberately exceed physical RAM together; decisions rest on kills and stall, not their sum.

On 2026-10-06 06:54 PDT, two hung hana tests in one local `verify.sh test` step held 11.4 GB and 15.5 GB; earlyoom first sent SIGTERM to 26 other processes, including six CI compilers (four hana CI jobs failed), and killed the tests last. `builds.slice` peaked at 37.7 GiB, under its 44G, because zram (10 GiB), sessions (8 GiB) and CI (3.4 GiB) had already taken the machine to earlyoom's 5% line.

### The verify run place

The outer Linux `verify.sh` sets `BUILDLOG_RUN` after detachment.
The value contains its PID and the start-time string from `/proc/<pid>/stat`, separated by `:`.
Nested calls retain that value.
The Mac uses `NoRun` and retains claim admission without reading `/proc`.

`queue_place.py admit` inherits the exported `BUILDLOG_RUN` through `memory_gate.sh`.
The client parses the value once into `RunIdentity` or `NoRun`.
A missing or malformed value gives the step its own place.

The first step creates one `RunPlace` with its arrival time and its `Owner`.
The service sets `order_since` on snapshot row copies from the run place.
It uses the earlier of the run time and the step arrival time.
Durable waiter records retain only the step arrival time.
An absent wait retains its run identity so it can use the run place while it exists.
Each step retains its own `since` for reconnects, waiting age, token reacquisition, and AWS eligibility.
Each step also retains its own owner, episode, and reservation.
Queue priority remains the first ordering field.
A CI step uses the same `order_since` field for its job start; see "CI cargo admission".

The service retains the run's process watch between steps.
The run holds memory for a 10-second gap between steps. It holds no CPU token during that gap.
When the run ends, waiting steps use their own arrival times for order again.
The service persists run places with waiter places through the generic codec in `state.json`.
After a restart, the service restores live run places and removes ended run places.

### The admission service

`scripts/build_queue/admission_service.py` is the only live admitter. Its epoll loop owns the queue and reservations.
`admission_protocol.py` defines every request and reply. `admission_client.py` handles connections, retries, wait output, and bounded queries.
`step_estimator.py` owns the estimates. `admission_records.py` owns the shared records, configuration, and wait phrases.

Each Builds page row and each queue wait line shows `<owner>: <what>`, such as `nateroids: nextest`.
`snapshot_codec.row_text(owner, what)` is the one function that makes this text.
`snapshot_codec.ROW_SEPARATOR` is `: `. A row with no usable "what" shows only the owner.
On the Builds page, `queueRowLabels` in `memory_live.html` gives the row start and the small line under it. A row with a unit starts with the unit.
A unit at the row start always shows in the unit color, by its role, also when it has no staff entry. `StaffNames.unit_open` makes the opening tag of that span with `role_span_open`, the same code as `StaffNames.html`. The feed sends it as `unit_open`, and `staffNames.unit(text)` in the shared script adds the name.
A row with no unit starts with its owner, the text before `: ` in its name. The text after `: ` goes in Step.
An owner with a staff entry, such as a showrunner, shows in its staff color. Another owner, such as hana CI, shows in plain ink.
A row with no unit has no small line, because the line would repeat the owner.
A Mac row with no unit has a `started_by` field instead, so it shows in the same form as a unit row. Its owner starts the row (a staff owner, then the launcher). Its repository is the small text, and its step is in Step.
The "what" is the caller's own text when the request identity has a `what` field, and else the step name. `mac_queue_run.py` writes its `--what` text as the `what` field of the `--assignment-file` JSON, next to `episode` and `pid`. `place-step` reads that field before placement and the queue record keeps it, so a Mac run row shows, for example, `cargo-liner: run 6 installs`. An assignment file with no `what` keeps the step name.
`snapshot_codec.owner_name` gives the owner of local, remote, cloud and Mac rows, in this order:
1. The recorded unit, then the staff unit.
2. The staff showrunner, or the session name for a session with no production.
3. The stored caller name, then the Mac launcher, then the repository or package.
A CI row uses `hana CI` as the owner.
A gate job that the feed cannot read shows only its owner, from `owner_name`, on a local, remote or cloud row.
So each of these rows removes the showrunner prefix in the same way, for example `merge-gate` for `sdlc-merge-gate`.
`slot_wait.QueueRowName.owned` and `memory_feed.row_name_parts` use `row_text`, so the name and its parts agree.
An absolute repository path does not supply a project name.
Matching and history retain the original step name.
The queue omits a live unowned scope when its run has no reservation and keeps no memory.
A run with a memory hold shows through that hold.
The services block and the block for processes from ended builds remain visible.
The memory page keeps this memory in Other.
`memory_admit.py` owns the readers, pure admission rules, projection, and finalizer.

[Free memory, head protection, and step slots](#free-memory-head-protection-and-step-slots) defines the room, keep-asides, and cap.
The first two minutes use the same calculation. A run never counts its use twice.
The display shows zero room for a shortfall. The cap explanation shows the shortfall.
These readings never reduce a promise from history.

Waiting steps follow priority, first run arrival, step arrival, and PID order.
A younger run starts only when its need fits after the first run's need remains protected.
The first run can start despite insufficient room when no admitted run or held slot remains.
Readable host memory, clear readable pressure, and a current estimate remain necessary.
New starts have five seconds of spacing through `recovery_start_seconds`.

An ended step retains its slot for `slot_gap_seconds`, currently ten seconds.
A continuation reuses that slot without start spacing, even under pressure.
A larger continuation requests only the extra memory before waiting runs.
That extra requires sufficient room and clear readable pressure.
The gap deadline or the run's process end releases an unused slot.
A closed connection ends its wait and releases its claim.

Every pass reads one `MemoryConfig` from `[memory]` in `config/build_admission.conf`.
`read_admission_configuration()` returns `UsableAdmissionConfiguration` or `UnusableAdmissionConfiguration`.
`read_memory_config()` resolves each key through `ConfigRead.origin()` once.
It collects recovery diagnostics and emits them only after a successful read.
Estimates receive the cargo peak and history factors from that configuration.
A recovered setting remains usable and produces one warning per process.
An unavailable setting stops grants and returns a refusal that names the file and key.
The service listens during a refusal and watches the nearest existing configuration directory.
`scripts.lib.inotify` reports directory replacement, permission repairs, completed writes, and overflow events.
Each relevant event re-arms the watch before a configuration read; no stamp filter discards repairs.
Watch registration tries each ancestor and tolerates filesystem errors.
When no ancestor accepts a watch, the service retries after the last usable source-check interval or the protocol cleanup interval.
Only configuration resolution decides usability; watch errors do not stop cleanup.
The service retains the last usable `MemoryConfig` for transport bounds, timers, and replay retention.
Cleanup requests have bounded strings and a protocol size limit, independent of the configured request limit.
Persistence reads no admission configuration.
Program places retain their acquisition configuration for reservation endings and scope cleanup.
Standalone cleanup uses the timing saved with its grant, or the fixed protocol timing when no saved timing exists.
The service removes its AWS queue snapshot during a configuration refusal.
The pass uses that policy for readings, decisions, and explanations. Its stored inputs preserve the evidence for counterfactual release advice.
Queries project the stored `PassResult` through `queue_read_from_pass`. A release marks the stored result stale; the next query computes its evidence once without granting a step.
`queue_read()` supplies one `QueueRead` for the complete queue page.
A failed query raises `AdmissionUnavailable` within the query bound. Commands print `build queue: admission service unavailable: <reason>` and exit 2.
`/build:page` shows the running and waiting steps, memory use, wait reasons, priority, and cloud slot counts.
The Builds page shows the saved Mac idle alert first, in red.
The feed reads it through `mac_idle.view_alert_line()`, which also checks the live Mac state, and sends its text in `mac_idle_alert`.
An empty value hides the alert on the page.
The Build limits section shows one facts line under each machine heading, in `<div class="build-limit-facts">` below the state.
The feed reads `machine_facts.read_facts(machine_facts.state_path())` once per render and passes it to `limits_view.build_limits(..., facts=)`.
`limits_view.facts_line()` writes each line into `MachineLimits.facts` with the `facts_*` words in `PAGE_WORDS`.
Examples: natedev `load 52.7/32 · battery 100% charged · not in use`, Mac `link 3 ms · load 19.6/12 · battery 85% · in use`, AWS `no box running`.
natedev has no link part. A machine without a battery has no battery part.
On mains a battery is `not in use`, and a full one shows `battery 100% charged · not in use`. Without mains it shows `battery 54% · in use`.
A failed TCP connect shows `link unreachable`. A failed SSH command after a good connect shows `link 3 ms · load unreadable`.
A UPS that `upsc` cannot read shows `battery unreadable`. A Mac without an `InternalBattery` line in `pmset`, or a machine with no UPS tool, shows no battery part.
A running AWS gate box shows `box running · no link or load reading`, because its security group accepts no inbound connection.
A probe that cannot start shows `facts unreadable`.
A missing or invalid file reads as `machine_facts.FactsUnread.VALUE` and shows `facts not sampled`.
Facts older than `machine_facts.STALE_SECONDS` (three timer periods) show `facts not sampled since HH:MM` in the user's zone.

A Mac claim without a repository row shows under the Mac heading as `1 in use`, with a run mark.
A kept claim (`mac_test.run_is_kept()`: the leftovers of a stopped run) shows as `1 kept (leftover of a stopped run)`, and two or more show as `2 kept (leftovers of stopped runs)`. Both kinds show as `1 in use · 1 kept (leftover of a stopped run)`.
A kept claim has the kept mark, a filled square (`.build-limit-mark.kept`), in place of the run mark, under the heading and in a repository cell. The legend names it `A place kept by a stopped run`.
`limits_view.read_mac()` carries the kind as `MacClaimUse`. A kept claim still counts in the limit. A repository cell counts it in `used` and in `kept`, and its hover names it under `Places`.

The feed uses `head_facts.priority_text()` for the page's `priority_text` line.
A waiting step's hover puts each field on its own line: the wait time, the reason, and `can run on`.
`can run on` is one line that names the targets that can take the step now.
A local step shows `natedev or Mac`, `natedev only`, or `natedev only (Mac state unknown)`.
`MacRoutes.fit()` in `target_availability.py` gives the Mac answer, and `memory_feed.local_runs_on()` makes the line.
AWS runs only the ~/.claude test gate. A cloud gate shows `AWS (natedev only when AWS cannot start the gate)`.
A step with a Mac Linux guest candidate names its placement candidates in `can run on`, from `memory_feed.gate_placement_notes()`.
A candidate with a `FixedExclusion` is not named, because placement never uses it.
When the guest has only a `FixedExclusion`, the line uses the route fit above.
When memory holds the first step in line, its hover adds a `First in line` table.
The table starts at free RAM and subtracts each term that is not zero: the safety net, swap coming back, build reserved, and RAM held for other waiting steps.
It ends with free and the need.
`queue_evidence.room_terms()` gives the memory amounts to the Builds page.
The memory bar divides the machine's RAM into three groups, in this order:
1. Builds: build tools, queued builds, and hana CI.
2. Free RAM: build reserved, free, and system reserved. System reserved is the safety net and swap coming back.
3. Everything else: the desktop, agents, other programs, the kernel, the cache, and RAM that is not attributed.
Brackets above the bar name each group and the three parts of free RAM.
A bracket under the bar shows the queue cap, from 0 to the end of free.
`RamBlocks.cap_bytes` is that cap: builds, build reserved, and free. The page's taken amount is the cap less free.
The hana CI amount is its swap-backed use from `ci_cap_use`, the same amount that admission counts.
The legend shows each group under its own heading, and the queue cap rows sit under Builds. The key for waiting steps and build progress sits at the bottom of the Running and Queued card, under the chart it explains.
The status line under the free amount names one cause, in this order:
1. An unavailable reading.
2. Memory pressure.
3. Nothing free: a shortfall, or reserves that use all free RAM.
4. The GPU test slot. When free does not cover the step's need, the line also names the need and free.
5. One running build that leaves too little free.
Use `/build:page` for the queue view and admission evidence.
Use `/build:bump <position|session|folder>` to set a waiting unit's priority.
Use `/build:bump clear [unit]` to remove priority.
`queue_evidence.py` formats the recorded memory amounts for the Builds page and memory reports.
Admission and release calculations remain in `memory_admit.py`.
The codec supplies an inactive legacy `QueueInputs.override` field for pinned clients.
An expired estimate requires a client refresh through the service.

| Request | Result |
| --- | --- |
| `admit` | Wait, refusal, or a durable grant with reservation id, `.max`, `.wave`, and sampling context |
| `launch` | Wait, refusal, or launch permission without a reservation |
| `release` | Finalize one reservation |
| `width` | Record the requested test width |
| `token_wait` | Finalize a reservation and preserve its queue place |
| `priority_set`, `priority_clear` | Change the one unit priority |
| `view`, `explain`, `mine` | Read the stored pass |

`/build:bump <position|session|folder>` selects a waiting step by position, session, or folder.
It sets priority for that step's unit. An unmatched selector exits 3 without a priority change.
The service refuses `priority_set` when no waiting step or current reservation belongs to the unit.
`/build:bump` works from any session or a plain shell.
`queue_command_actor()` selects `CLAUDE_CODE_SESSION_ID`, then `CODEX_THREAD_ID`, then `"user"`.

Mutation requests have replay ids. The service persists each terminal reply before delivery.
A replay returns the same reply. Live grants remain in the ledger until their reservation ends.
Ended replies expire under `replay_retention_seconds`. Wait replies remain only on their connection.
Each state write also removes an ended placement episode: no current attempt, an owner that is not live, and no request or attempt report newer than `ended_placement_seconds` (600 s).
It removes the request and every closed attempt of that episode.
`Wait` carries the decision, readings, need, policy, and `ConfigurationDiagnostic` records from the same pass.
Client commands print each recovery warning once per process.
Unavailable settings produce refusals rather than replacement policy values.

### Durable state and lifetime

`admission/state.json` holds reservations, waiter places, episodes, queue priority, start spacing, and the replay ledger.
It also holds placement requests, assignments and current attempt keys.
`SavedPlacementAssignment` retains assignment state, launch permission, exact provider reference, consumed holder identity, and retry decision.
It also retains a terminal report intent until fresh provider evidence permits settlement.
The consumption ledger key contains the episode and attempt, independent of the transport request id.
The ledger stores status, never a replayable authorization.
Current attempts retain their ledger entries through the existing replay retention path.
A pruned or unknown attempt cannot create another permission.
Restart restores unconsumed and consumed attempts without changing their identities or queue times.
Only checked local submitting owners receive process watches.
Remote execution identities retain their host and generation, separate from those submitting owners.

Each provider evidence key includes the session, destination, scope, command policy and configuration generation.
Each background reader publishes immutable evidence through the service wake path.
The loop takes no provider lock and performs no remote probe.
Evidence expires after the existing waiting poll interval plus the target probe timeout.
Stale evidence prevents a new decision that needs it; it never releases a provider claim.
Mac and SSH records retain their claim id, episode, attempt and submitting session through adoption.
Release compares the exact claim identity under the provider lock.
The Mac claim count uses `build_slots.configured_maximum` to read the repository limit or host default in `build_folders.conf`.
The mac-linux gate shares the claude_commands repository limit with native Mac runs, even from a differently named checkout.
Each guest uses a numbered claim. Mac mirror work uses the unnumbered claim.
The Mac resolves the repository before it takes the claim lock.
The key follows local clone origins to the upstream repository. Git alternates supply objects only.
The Mac runner reads the step history key from the submitting checkout. This read includes Git worktree URL rewrites.
The Mac runner uses the shared origin to find the mirror and set the claim key.
Each new work claim saves its claim key, history key, claim file, mirror name, and start fence directory.
The key determines the file name for a new claim. The mirror name identifies the directory the runner uses.
The runner, release, cleanup, and execution observer use the saved identity. Handoff retains the reservation file and its start fence.
A handoff can adopt an exact cleanup claim after the checkout is gone.
A legacy Mac claim with no key blocks all repositories. Any Mac caller can take the exact claim for cleanup after the saved owner ends.
A claim without a proven mirror or history key authorizes cleanup only.
The runner takes the execution lease before it replaces an incomplete local reservation.
The claim child publishes the replacement and removes the old reservation under one provider lock.
The runner keeps the execution lease through the run.
Cleanup uses the saved mirror lease when the claim identifies the mirror. Otherwise, it takes the host lease and counts processes in all mirrors.
The idle watcher reads an expired remote claim again under its cleanup lease before release.
If a cleanup lease cannot open or its mirror root is invalid, the watcher skips that claim for the current pass.
Shell and Python runners take a shared host lease before they take a mirror lease.
The host-wide count sends no process signals. Unrelated mirror work can keep the claim pending.
The Mac build log and the queue use the same Git root key for history when a repository has no origin.
Held slots and waiting extras remain in memory. A restart removes them and retains the run's queue place.
The service replaces this file atomically. Reservation records retain accepted needs, run identities, scope paths, and finalization intent.
Width and call absence use named variants. `RequestedTestWidth`, `UnspecifiedTestWidth`, and `NoTestWidth` describe the width.
`CallRecord` and `NoCallRecord` describe the call.

`reservation_stem` gives each grant its `.max` and `.wave` paths. `invoke.sh` uses the grant's paths and sampling context.
The sampler reads no reservation file. It marks the nextest test phase through `record.py test-phase-context`.
The service watches owner pidfds and scope events. A populated scope retains its accepted need after its owner ends.
An empty scope and ended owner permit finalization. A reused scope path gets a new watch.

`finalize_reservation` is the one finalizer. `StateIntents.save` persists its evidence before the history append.
The finalizer matches `reservation_id` to prevent a duplicate history row after a crash.
`StateIntents.clear` removes the reservation after the append. Startup completes any saved intent.
The finalizer records completed measurements for history. Release creates the run slot without changing the accepted need.

At the first start without `state.json`, the service imports the old admission files through their existing readers.
It imports live reservations, saved intents, waiter places, token handoffs, priority, and start spacing.
It writes state before it deletes imported files. Later starts import nothing.
The old `.waiting`, `.reservation`, `.token-wait`, and `queue_priority.json` forms have no live writer.
There is no admission lock.
`hold_enforcer` imports `admission_service` inside `admitted_processes()` when it reads durable grants.
Importing `build_hold` or `build_targets` does not load the admission service.
`ServiceState` and `read_state` retain one definition in `admission_service.py` because the state uses several service types.
The service imports `argparse` inside `main()` and the reading alert message helpers inside `deliver_reading_alert()`.
After each pass, the service publishes `admission/queue.json` atomically through `scripts/build_queue/queue_snapshot.py`.
The snapshot contains the pass readings, its policy, and the remaining waiters.
The AWS controller reads this snapshot without a live queue request.
It refuses a start when the snapshot is missing, stale, or incomplete.
Queue views continue to use the service.

### Queue lifecycle and calibration

The service records queue events in `admission/queue_times.jsonl` through `append_queue_time`.
The same record holds Cargo, `test-affected`, and `gate` steps.
The append function locks the file and removes incomplete rows before an append.
At 8 MiB, it retains the newest events from the last 14 days in less than 6 MiB.

1. `admitted` records the original arrival, admission time, wait seconds, need, need source, and whether the step is first.
2. `left` records a cancelled waiter or a waiter whose owner is gone.
3. `ended` records the reservation's end time, outcome, end kind, and sampled peak state.

Each event identifies the logical step by `pid`, `start_time`, and the original `since`.
Each admission and end also identifies its reservation interval by `reservation_id`.
A token wait ends an interval with `TokenReturn`. It does not complete the step.
A normal end uses `StepEnd`. An exit before a positive sample records `NoPositiveSample`.
The saved finalization intent retains the facts for the end event across retries.
The event identity prevents a retry from publishing the same end twice.
The service saves pending admission and cancellation facts with each durable state transition.
One lifecycle outbox publishes these facts after the state write and during restart.
Reservation removal and removal of its non-token place use the same state write.

`read_queue_lifetimes` joins these events into `QueueStepLifetime` records.
Each record retains its reservation intervals, token gaps, and final end state.
Memory wait includes the first wait and each wait after a token returns.
It excludes the token gaps and running time.
A missing return time gives `MemoryWaitIncomplete`; an unfinished step gives `StepStillOpen`.
The Phase 16 comparison is due 24 hours after these lifecycle records reach main.
It compares Cargo waits and pressure holds with and without overlapping gate intervals.

`head_policy` resolves the deployed head rule and ordered walk from one frozen snapshot.
Admission, queue readers, blocker searches, and release searches use that result.
The head keeps its accepted need while it waits.
Younger runs can start only if they leave room for that need.
The rule applies with and without queue priority.

The Phase 20 calibration has insufficient evidence.
The calibration requires seven days under the deployed Phase 39 configuration.
The phase review found no eligible days or cohort budget after that deployment boundary.
Recorded wait minutes, replayed wait minutes, and their ratio are unavailable.
The main reference has a 24 GiB build budget; this is not a calibration cohort.
The comparative replay does not run. The capacity rule is not adopted.

### Estimates and keep-asides

The client imports `step_estimator` inside `step_request`. It calls `step_estimate` before admission and after `EstimateExpired`.
`system_clock.py` owns the shared `SystemClock`. Cleanup commands load no service or queue view.
Before each new build estimate, it reads `build_history` from the service, then reads history.
`StepProfile.build_history_generation` keeps that generation through reconnects and delayed replies.
The service grants a build only when its estimate generation equals `ServiceState.build_history_generation`.
A missing generation or a different generation gives `EstimateExpired`.
A matching pending finalization also gives `EstimateExpired` until history publication finishes.
`StateIntents.published` advances the durable generation after the history append and before intent removal.
A publication retry also advances the generation. The service never prunes or resets the generation.
A refresh uses the open admission connection. An unregistered client uses a read-only `build_history` query.
A pass retries a retained finalization intent even while its owner remains alive.
It preserves the first `since` and episode across refreshes and reconnects.

With `--wait-events`, the client emits `first`, then `holder <json>`.
It emits another holder event only when the holder changes.
The JSON identifies a session (`kind`, `session_id`, `session_name`), a unit (`kind`, `unit`), or no holder (`kind=none`).
The pass records the holder in `AdmissionWaitSnapshot.holder`.
An order hold names its protected waiter, including a continuation that requests extra memory.
A room wait names the run with the largest recorded charge, including held slots.
A held slot keeps the run's recorded unit after its reservation ends.
The charge uses grouped swap-backed use and the current keep-aside.
Unavailable run use retains the full keep-aside charge.
Unregistered CI jobs never become holders.
Pressure, unreadable host readings, start spacing, and estimate refresh report no holder.
The holder defaults to `NoHolder` when an older service omits the field.
An older client ignores the added field and keeps its wait.
At a service restart, the client reconnects with the same episode and arrival time.
It prints `admission service restarted; waiting again` once when the connection closes.
The existing unreachable limit still applies until a valid reply arrives.
`QueueInputs` and the queue snapshot version remain unchanged.

A fresh nextest estimate expires after one running-pass interval. Other history estimates expire after `estimate_refresh_seconds`.
A program uses its history estimate and refreshes it under the normal estimate policy.
The merge gate reads `program_first_peak_gib.gate` at use; its first need is 4 GiB.
Legacy gate peak records do not size that need. A matching anonymous progress profile supplies the first pass's look-ahead window.
The gate removes its progress counter before reruns, which keep the full need.
The merge-gate progress profile key includes `shape` (`tests` or `types`).
Earlier profiles do not match, and nightly runs use separate profiles.
It reads `merge_gate_governor_gib` when the gate starts; the initial value is 4 GiB.
The governor bounds module starts when a CPU slot is free; with no job running, it always permits one job.
Memory peaks do not kill jobs or fail the gate.
The gate reports its first need as a fallback and explains any matching anonymous progress window.
The queue model reads the step names once from `config/build_steps.conf`.
Renderers use its `StepNames` value without file reads.
Other sentences read the step names when they need the text.
The Builds page uses the configured step name, including `test affected` for `test-affected`.
`step_promise` gives every admitted step a `LifetimePromise` until it ends.
The run calculation uses the accepted need before tests start.
For nextest, an observed test phase can select the frozen test-phase need instead.

Other programs retain `HistoryPeakSizing` and `program_peak_estimate`.
Cargo estimates separate compile history from no-compile history.
`index_step_evidence` joins each anonymous peak by host, call id, and step.
`StepEvidence` keeps its shape and compile count independent.
For a test step (`TEST_STEPS`: `nextest`, `test`, `t`) or a `check` step, it also keeps the decoded argv as `arguments`.
`history_selection` reads it for test steps, and `same_check_targets` reads it for check.
`history_selection` gives each anonymous row its package, scope and filter set.
A recorded field wins. Only a missing field is parsed from `arguments`, once, through `test_selection`.
A row with neither keeps the recorder defaults: no package, `full`, no filters.
Missing argv does not remove a compile count. A saved shape does not prove compilation.
`CompiledCrates` identifies a compile run. `NoCratesCompiled` identifies known zero-count attempts.
The join combines repeated attempts. Any positive count keeps the call in compile history.
An unknown count or a missing join keeps the peak usable until the index supplies a known count.
The peak's completion time controls its history window, including failed-build floors.
The estimate reader decodes only rows that can be in the window (`HistoryWindow` in `step_estimator.py`).
Rows append when steps end, so a binary search finds the first row that ended one day before the window.
The day covers rows that came late; on 2026-11-15 the latest came 19 minutes late. A damaged row stops the search there.
A row whose bytes do not hold the host and a requested step is not decoded.
Merge-gate peaks keep their separate class and remain in the anonymous history.
The gate uses its own cap, so Cargo estimates do not use those peaks.

Five keyed rows exclude the older unkeyed rows for the same repository.
`scripts/buildlog/repository.py` owns the repository identity rule for the recorder and estimator.
A Git tree keeps its normalized origin key or its first root commit key.
A directory without Git uses `manifest-path:<real Cargo.toml path>`.
The step's `--manifest-path` selects its manifest folder when present.
Otherwise, the reader selects the nearest `Cargo.toml` in the current folder or its parents.
The reader starts no command for a manifest identity and does not require Cargo on `PATH`.
Each manifest keeps its own key; the reader does not resolve a workspace root.
The identity resolves symbolic links and keeps separate manifest paths separate.
A folder without a manifest keeps the unknown identity.
An empty Git repository keeps its unknown identity until a remote or root commit supplies a key.
The estimator uses the manifest folder name without matching older rows by name alone.
The first build keeps the configured Cargo fallback of 12 GiB.
A recorded peak supplies the next estimate through the same identity and build shape.
The estimate retains the configured headroom rules.
Remote verify mirrors retain a supplied source identity when the local identity is unknown or comes from a manifest path.
With one to four own rows, a non-build estimate uses their largest peak or a lower ranked pool peak.
A repository without own history keeps the pool and fallback order.
A full test estimate with a package keeps only rows whose package and scope match.
Other steps, and requests with no package, keep the recorded scope rule.
A check estimate keeps only rows whose check targets match the request (`check_targets`): `--all-targets` or named targets.
Test code raises the check peak, so an all-target check never takes its need from lib and bin checks.
A check peak without recorded arguments has unknown targets, so no check estimate uses it.
A new peak joins its estimate when the buildlog index records its arguments.
A filtered compile estimate (`filtered_history_peak`) takes the larger of two ranked anonymous peaks:
the exact filter set's rows and the package's filtered rows.
Indexed peaks times `ANON_SHARE` come next, exact filter first. Check or build compile peaks come last.
A full-suite estimate does not cap a filtered compile estimate.
The 1 GiB filtered floor applies when the repository has no usable compile history.
Fresh nextest estimates retain `fresh_test_peak` and its 1 GiB floor.
Freshness still requires both Cargo output roots to survive.

The Phase 18 replay found no gain, so no estimate splits its history by build feature or commit.

#### Phase 19 source shares

Run date: 2026-11-05.
The snapshot ends at 2026-11-05 13:07:40 UTC.
The requested window starts at 2026-10-29 13:07:40 UTC.
The lifecycle file retains 571 events across 4.92 days, including older token-gap records.
The 220 admissions cover 2026-11-04 20:45:52 through 2026-11-05 13:03:36 UTC: 16.30 hours.
The anonymous file retains 46,262 rows from 2026-10-07 through 2026-11-05.
The requested window contains 5,205 anonymous rows.
Replay reconstructs 4,267 estimates and keeps 815 merge-gate cap rows separate.
Another 123 rows lack their own dated index match.
Recorded intervals and reconstructed estimates are separate populations. Do not add their counts.

| Population | Step kind | measured | pooled | buildlog | fallback | shape | cap |
|---|---|---:|---:|---:|---:|---:|---:|
| before Phase 60 | full nextest | 0 | 0 | 0 | 0 | 0 | 0 |
| before Phase 60 | filtered nextest | 0 | 0 | 0 | 0 | 0 | 0 |
| before Phase 60 | clippy | 6 | 0 | 0 | 0 | 0 | 0 |
| before Phase 60 | check | 0 | 0 | 0 | 0 | 0 | 0 |
| before Phase 60 | build | 0 | 0 | 0 | 0 | 0 | 0 |
| before Phase 60 | doc | 2 | 0 | 0 | 0 | 0 | 0 |
| before Phase 60 | other | 6 | 0 | 0 | 0 | 0 | 9 |
| before Phase 60 | **Total** | 14 | 0 | 0 | 0 | 0 | 9 |
| after Phase 60 | full nextest | 6 | 0 | 0 | 1 | 0 | 0 |
| after Phase 60 | filtered nextest | 0 | 0 | 0 | 0 | 0 | 0 |
| after Phase 60 | clippy | 20 | 0 | 0 | 0 | 0 | 0 |
| after Phase 60 | check | 0 | 0 | 0 | 0 | 0 | 0 |
| after Phase 60 | build | 0 | 0 | 0 | 0 | 2 | 0 |
| after Phase 60 | doc | 4 | 0 | 0 | 0 | 0 | 0 |
| after Phase 60 | other | 19 | 0 | 0 | 0 | 0 | 145 |
| after Phase 60 | **Total** | 49 | 0 | 0 | 1 | 2 | 145 |
| before queue 10z | full nextest | 6 | 0 | 0 | 1 | 0 | 0 |
| before queue 10z | filtered nextest | 0 | 0 | 0 | 0 | 0 | 0 |
| before queue 10z | clippy | 26 | 0 | 0 | 0 | 0 | 0 |
| before queue 10z | check | 0 | 0 | 0 | 0 | 0 | 0 |
| before queue 10z | build | 0 | 0 | 0 | 0 | 2 | 0 |
| before queue 10z | doc | 6 | 0 | 0 | 0 | 0 | 0 |
| before queue 10z | other | 25 | 0 | 0 | 0 | 0 | 86 |
| before queue 10z | **Total** | 63 | 0 | 0 | 1 | 2 | 86 |
| after queue 10z | full nextest | 0 | 0 | 0 | 0 | 0 | 0 |
| after queue 10z | filtered nextest | 0 | 0 | 0 | 0 | 0 | 0 |
| after queue 10z | clippy | 0 | 0 | 0 | 0 | 0 | 0 |
| after queue 10z | check | 0 | 0 | 0 | 0 | 0 | 0 |
| after queue 10z | build | 0 | 0 | 0 | 0 | 0 | 0 |
| after queue 10z | doc | 0 | 0 | 0 | 0 | 0 | 0 |
| after queue 10z | other | 0 | 0 | 0 | 0 | 0 | 68 |
| after queue 10z | **Total** | 0 | 0 | 0 | 0 | 0 | 68 |
| Replay | full nextest | 655 | 23 | 0 | 3 | 0 | 0 |
| Replay | filtered nextest | 475 | 0 | 10 | 49 | 0 | 0 |
| Replay | clippy | 1429 | 20 | 0 | 0 | 0 | 0 |
| Replay | check | 426 | 13 | 0 | 0 | 0 | 0 |
| Replay | build | 0 | 0 | 0 | 0 | 149 | 0 |
| Replay | doc | 215 | 7 | 0 | 0 | 0 | 0 |
| Replay | other | 765 | 28 | 0 | 0 | 0 | 815 |
| Replay | **Total** | 3965 | 91 | 10 | 52 | 149 | 815 |

The Phase 60 boundary is its first origin/main ancestor at 2026-11-04 21:52:05 UTC.
The queue 10z boundary is its first origin/main ancestor at 2026-11-05 07:24:57 UTC.
Git merge times delimit cohorts. The records contain no process version, so these boundaries do not prove deployment times.

| Fresh tier | Full nextest | Filtered nextest |
|---|---:|---:|
| measured | 162 | 49 |
| buildlog | 0 | 0 |
| NoFreshHistory | 14 | 7 |

The fresh table contains replay subsets, not extra estimates. Recorded events contain no fresh-tier field.
A zero indexed compile count selects a fresh scenario; the records cannot prove that both output roots survived.
The 21 NoFreshHistory cases take the compile estimate. Fresh buildlog includes the fresh floor.

No recorded admission uses ANON_SHARE: 0 of 220, and 0 of 66 without caps.
Reconstructed ANON_SHARE share: 10/5,082 (0.20%); without caps: 10/4,267 (0.23%).
The 10 ANON_SHARE promises have 0 under-half cases and 1 over-three cases against their measured anonymous peaks.

| Replay step kind | ANON_SHARE share | Share without caps |
|---|---:|---:|
| full nextest | 0/681 (0.00%) | 0/681 (0.00%) |
| filtered nextest | 10/534 (1.87%) | 10/534 (1.87%) |
| clippy | 0/1,449 (0.00%) | 0/1,449 (0.00%) |
| check | 0/439 (0.00%) | 0/439 (0.00%) |
| build | 0/149 (0.00%) | 0/149 (0.00%) |
| doc | 0/222 (0.00%) | 0/222 (0.00%) |
| other | 0/1,608 (0.00%) | 0/793 (0.00%) |

Request selection evidence: 11 recovered legacy test rows and 1,204 rows with recorded fields.
Available fourteen-day test history: 3,655 recovered legacy rows, 1,250 recorded rows, and 39 rows with unknown selection.
All 149 replayed builds have shape evidence. ShapeFallback supplies 31 promises; these remain shape sources, not ordinary fallback sources.
Observed waits: 225 logical steps have recorded totals of 1,162.98 minutes.
3 steps have incomplete waits and contribute no value.
4 intervals remain open. No ended recorded interval has NoPositiveSample.
Phase 18 and Phase 68 are absent from origin/main at this snapshot, so their deployed cohorts are unavailable.
No evidence reaches 2026-11-12, when the legacy rows leave the window. An after-expiry share is unavailable.
The current estimator and configuration reconstruct earlier requests using only evidence available before each request.
The replay assigns each indexed attempt to at most one sample, including samples before the requested window.
Eight samples without their own indexed attempt are unresolved. The replay excludes them instead of using another attempt’s request time.
The earlier repairs corrected retry times and used the production repository identity rule.
Queue 10s changes no estimator function or configuration value in this comparison.
The showrunner stopped this phase at Spec 2 because the overall ANON_SHARE share is under 5%.
This measurement changes no estimator tier.

#### Phase 11 replay comparison

The replay covers 2026-10-21 through 2026-11-04 with the same frozen inputs.
It evaluates 15,053 runs and omits 195 runs without an index match.
Another 617 merge-gate rows retain their separate cap class.
Each estimate uses its historical budget: 21 GiB or 24 GiB.
Under-half means below half the measured peak. Over-three means above three times the measured peak.

| Compile class | Budget GiB | Runs | Under-half before | Under-half after | Over-three before | Over-three after |
|---|---:|---:|---:|---:|---:|---:|
| compile | 21 | 11,360 | 458 | 402 | 1,162 | 1,152 |
| compile | 24 | 2,300 | 18 | 12 | 332 | 358 |
| no compile | 21 | 1,185 | 12 | 12 | 937 | 937 |
| no compile | 24 | 208 | 3 | 4 | 174 | 173 |
| **Total** | | **15,053** | **491** | **430** | **2,605** | **2,620** |

These repositories have a higher count. Each row combines their steps, compile classes, and budgets.

| Repository | Under-half before | Under-half after | Over-three before | Over-three after |
|---|---:|---:|---:|---:|
| cargo-liner | 27 | 8 | 1,754 | 1,804 |
| check-only | 1 | 2 | 5 | 3 |
| clone | 1 | 2 | 15 | 14 |
| clone85 | 1 | 2 | 0 | 0 |
| fix | 3 | 3 | 20 | 22 |
| hana | 71 | 31 | 592 | 595 |
| lintproof_fix | 0 | 1 | 6 | 1 |
| p31-gates-834aebb04-8015c7caf3 | 1 | 2 | 3 | 1 |
| src | 0 | 2 | 46 | 40 |
| work | 1 | 2 | 0 | 0 |

Bar missed: under-half falls by 61, but over-three rises by 15.
At least one count rises in 10 repositories and 19 repository, step, and compile-class groups.
The largest rise is cargo-liner nextest compile runs at 21 GiB: under-half 27 to 7, over-three 101 to 166.
A filtered compile run takes its estimate from compile history, and the full-run estimate no longer caps it.
The showrunner accepted the miss (2026-11-04): an under-promise risks an out-of-memory kill; an over-promise only delays parallel starts.
Phase 60 found the promise source of these over-three runs and fixed it there; see the Phase 60 replay below.

#### Phase 60 replay comparison

The Phase 60 replay (`phases/60/replay.py`) rebuilds the Phase 11 replay with the same frozen inputs.
It classifies each call through `index_step_evidence` and `combine_compile_counts`. The baseline is `82f5c90d4`, the Phase 11 checkpoint.
The shared classification moves 51 runs from no compile to compile, so its class totals differ from the Phase 11 table.

The cause is in the selection of history rows, not in the tier order:
1. Anonymous rows recorded before 2026-10-29 have no package, scope or filter fields.
   `filtered_peak` rejected them, so 90 high promises came from indexed peaks times `ANON_SHARE`.
   Indexed peaks count memory outside the anonymous peak, so those promises were too high.
2. A full test estimate with a package mixed every package and older filtered rows: 79 high promises.
3. Three high promises came from large earlier samples. The fix keeps them.

The selector recovers missing fields from the indexed argv.
It selects full test rows by package and scope.
It uses the larger exact-filter or package ranked peak before any indexed tier.

| Compile class | Budget GiB | Runs | Under-half before | Under-half after | Over-three before | Over-three after |
|---|---:|---:|---:|---:|---:|---:|
| compile | 21 | 11,411 | 402 | 367 | 1,171 | 1,071 |
| compile | 24 | 2,300 | 12 | 12 | 358 | 361 |
| no compile | 21 | 1,134 | 12 | 12 | 918 | 920 |
| no compile | 24 | 208 | 4 | 4 | 173 | 173 |
| **Total** | | **15,053** | **430** | **395** | **2,620** | **2,525** |

Bar met: cargo-liner nextest compile runs at 21 GiB go from under-half 7 to 0 and over-three 166 to 65 (bar: 101 or lower, under-half 7 or lower).
At 24 GiB, the same group goes from over-three 6 to 2.
Hana over-three rises 595 to 607 while its under-half falls 31 to 23. Six other repositories rise by one or two runs in one count.

#### Phase 17 compile-band measurement (not adopted)

Phase 17 replayed nextest promises split by compile count (bands 1–5, 6–20, 21–50 and more than 50 crates) against checkpoint `e88fb841d`.
The band chosen from the last compile count of the same worktree and step was right for 54% of compile requests.
Promises under half the peak rose from 395 to 423, and ten repositories got worse. Promises over three times the peak fell from 2,525 to 2,485.
The adopt rule needs fewer under-half promises in total and no rise in any repository, so bands are not adopted and the estimate is unchanged.

Build estimates use completed same-shape history from the last 14 days.
Ten or more earlier runs supply a ranked peak. One to nine earlier runs supply their largest peak.
`ShapeRankedPeak` records the run count and the share used to select the peak.
Stored `shape_ranked_peak` records retain that share; the decoder reads older `shape_p90` records as share 10.
The wire uses protocol version 3 and retains decoding for versions 1 and 2.
The encoder writes share-10 bases with the older `ShapeP90` tag and no share field.
The wire decoder reads older `ShapeP90` tags as `ShapeRankedPeak` with share 10.
Other shares use the `ShapeRankedPeak` tag and retain their recorded share.
A config change introduces those shares after deployment of the new service.
The newest failed-build floor can raise that base.
Each new estimate reads `[memory]` in `config/build_admission.conf` through `memory_config()`.
A dev build uses `ceil(base × dev_size_factor + margin_bytes)`.
`dev_size_factor` is 1.1. `dev_size_margin_gib` is 1.1 GiB, rounded upward to bytes.
The Phase 39 replay uses the original 126-build cohort and the 64 measured builds from the latest three days.
The cargo-berth increase sets the margin. The measured S2 build sets the factor.
The S2 history peak of 6.3 GiB gives a promise of 8.03 GiB without truncation.
Release builds use `release_headroom`, currently 1.314. A build without same-shape history retains the same-profile fallback without headroom.
The retry after a memory kill stays unchanged.

Each estimate stores its history rule, factors, and failed-peak floor.
`DevSizeHeadroom` stores its factor and margin.
`LegacyDevMultiplierHeadroom` reads older saved multipliers through the existing `dev` wire tag.
New estimates remain uncut. Historical `CappedAtBudget` and `CapNotRecorded` values remain readable.

Each pass reads the resident working set and swap for service reports.
Unregistered CI use and unowned scopes reduce host free memory. They do not create a run promise.
`ci_memory_slice` names the CI slice. A missing CI reading remains a labelled diagnostic.
`MemoryRoom.ci_use` stores readable unregistered CI use separately from admitted swap-backed use.
`MemoryRoom.kept` includes only run keep-asides.
Registered CI jobs reserve memory through admission.
An unregistered CI job has no memory reservation. Its limit is the systemd `MemoryMax=34G` setting, or 34 GiB.
CI RAM already reduces host free memory. The room calculation never subtracts it again.
The service does not require `memory.max` for admission.
[Free memory, head protection, and step slots](#free-memory-head-protection-and-step-slots) defines the cap.
The page shows registered CI use with admitted runs and unregistered CI use inside the cap.
The page and the queue text show CI use under 0.1 GiB in MiB, so idle CI never reads 0.0 GiB.
The page counts each CI job once in its taken total.
`RunningServiceJob` and `IdleService` retain the CI job state.
`AnonymousRunUse` stores resident swap-backed memory: anonymous memory plus shared memory, which includes tmpfs files.
Scope readings use `memory.stat` `anon + shmem`.
Process readings use `/proc/<pid>/status` `RssAnon + RssShmem`.
`ProcessAnonymousUse` retains each process's contribution. Both keep their names because the snapshot and protocol codecs use the class name as the record kind.
The resident working set stays `max(0, memory.current - inactive_file)` and already includes shmem.
The reader does not add shmem to that working set again. It records swap separately.
Missing, invalid, or negative counters return `RunUseUnavailable`.
The shell peak sampler uses the same `anon + shmem` basis for step and test-phase peaks.
The queue-place sampler uses this basis for scoped programs and unscoped runner programs.
Its other unscoped program peaks retain their resident-memory basis.
CI subtraction removes registered scope use on that same basis, including shmem.
Nested registered scopes enter the subtraction once. Process subtraction excludes members already covered by those scopes.
An unavailable reading keeps the full accepted need aside and makes the cap unavailable.
The service freezes `TestPhaseNeed` and its margin when it writes a nextest reservation need.
A width update selects history again at the recorded width.
`test_phase_need` uses the existing history selector through `store.locked_rows`.
The selector uses the ranked test-phase peak from matching repository, scope, package, filters, and width history.
`test_phase_margin=1.2` has range `1..`. Missing or invalid values use `TEST_PHASE_MARGIN_FALLBACK`, which is 1.2.
The config reader resolves the margin once. Each reservation retains its selected margin.
`record.read_test_phase` supplies the observed test window and test peak.
After tests start, need equals `max(test peak so far, stored test-phase need) × stored margin`.
Without a stored need or observed test window, need remains the full step estimate.
The existing keep-aside formula remains `max(0, need − swap-backed use)`.
For a 12 GiB step, 3 GiB test use, and 3 GiB history need, margin 1.2 gives 3.6 GiB need.
That run keeps 0.6 GiB aside instead of 9 GiB.
The Builds page uses the keep-aside from the last admission pass.
Build-why explains the selection: `tests started: need X (test peak Y, history N x 1.2)`.
No test-phase estimate worker remains.

### Clients and program places

`memory_gate.sh` starts one foreground client for an admission wait. The client writes quoted grant assignments for the shell.
Its stdout remains one protocol grant. Release, width, and token handoff use client commands.
The Mac branch retains claim admission without a socket or `/proc` read.
BRP launches use `launch`; launch permission creates no reservation.

Transport failures never permit a start and never select another admitter.
The client reconnects with its first request identity. It reports unavailable transport periodically and waits.
The client checks the staff name, the exported name, and the session folder name in that order.
`admission_records.request_owner(worktree)` finds who started a request. It uses the first rule that matches:
1. A session (`CODEX_THREAD_ID` or `CLAUDE_CODE_SESSION_ID`), `BUILDLOG_OWNER_STAFF_ID`, or `CLAUDE_SESSION_NAME`.
2. The process family, through `family_owner(pid, proc_root)`: a state-root script, a terminal, or a desktop or login scope.
3. The recorded owner of an absolute worktree, through `copies.recorded_owner`: an active gate, the copy record, or the staff worktree.
   `admission_records.folder_owner` reads with `RemovalIntentAccess.READ_ONLY`, so a queue request never clears a removal intent.
   `copies.source_owner` keeps the default `RECOVER` read, so its other callers still clear a stale removal intent.
4. Otherwise the result is `OwnerNotGiven(program)`, which says only which program asked.
`family_owner` reads the process and its parents in `/proc` once, with the cgroup unit of each process.
The read stops at the first `claude` or `codex` process, so a session launcher never names the work.
A read error stops the read, and the next rule applies.
The script of a process is its first argument that is not an option and ends in `.py`, `.sh`, `.zsh` or `.bash`.
The read skips the value of `-X`, `-W`, `-o`, `+o`, `-O` and `+O`.
A relative script path starts at the `/proc/<pid>/cwd` folder of that process.
The function returns one type for each result, and uses the first rule that matches:
1. No process can be read, for example on macOS, which has no `/proc`. The result is `TERMINAL_OWNER`, named `terminal`.
2. A family process runs a script under the showrunner state root, in `<root>/<id>/...`.
   The result is `StaffOwner(member_id, name)`, for example the staff member `cargo-liner`.
   `request_owner` and the service resolve the staff id, so the row keeps the staff unit.
   When no staff member has that id, the result is `StateSessionOwner`, named with the first 8 characters of the id.
   `scripts/lib/showrunner_state.py` `state_root()` gives the root for this rule, `record_store` and `showrunners`.
3. A family process has a tty, and the read did not stop at an agent: `TERMINAL_OWNER`.
   A user terminal can run under a service, such as the `herdr-agents.service` terminal server, so this rule comes first.
4. A family process runs in a systemd `.service` unit. A timer that runs `systemd-run --user --scope` matches here.
   For a unit that `systemd-run` made (`run-*.service`), the result is `OwnerNotGiven("systemd · <outermost script file>")`,
   else `systemd · <folder of the process cwd>`. For another service, the result is `OwnerNotGiven("systemd · <unit>")`.
5. The process runs in an `app-*.scope` or a `session-*.scope`, and the read did not stop at an agent: `TERMINAL_OWNER`.
   A desktop app or a login session started the work, so the user owns it.
   For example, Zed starts rust-analyzer through `queue_place.py` in an `app-dev.zed.Zed-*.scope` with no tty.
   A desktop that starts an app in an `app-*.scope` puts no service in the family.
   KDE starts some apps as an `app-*.service`. Rule 4 matches that family, so it does not say who started the work.
   The flycheck cargo calls of rust-analyzer run in its `builds.slice` scope, so the cargo wrapper starts them with no queue request.
6. Other callers: `OwnerNotGiven("script · <outermost script file>")`, else `script · <command of the process>`.
The service uses the same function for a scope with no recorded caller, and shows the program text.
It reads the family of the oldest scope process.
`admission_client.refuse_unowned` refuses an `OwnerNotGiven` request in `step_request`, before the request enters the queue.
`admission_client.py place-step` prints `OWNER_REFUSAL` and exits 2. The text names the program and says to set
`BUILDLOG_OWNER_STAFF_ID` to a staff id, or `CLAUDE_SESSION_NAME` to a name when no staff member owns the work.
`queue_place.py`, and the cargo wrapper through it, prints `memory admission check failed (<OWNER_REFUSAL text>); step refused`
and `queue place: ...`, and exits 1.
The client refuses these callers when they set no owner variable and the worktree has no owner record:
- A family with a process in a systemd `.service` unit and no tty, such as an agent probe under `systemd-run` or a timer.
- A family under an agent with no session.
- A family in another scope, such as a `run-*.scope`, with no tty.
The documented probe form `systemd-run --user --scope ...` passes: the scope keeps the environment of the caller,
so the session variable of the caller names the owner.
The service form `systemd-run --user --wait --pipe ...` starts with the environment of the user manager, which has no owner variable.
For raw cargo or `verify.sh` in a folder that has no owner record, add `-E CLAUDE_CODE_SESSION_ID` or `-E CLAUDE_SESSION_NAME`.
These callers always give an owner, also under systemd or with no session:
- The merge gate and the nightly gate: `default_owner(<unit>)` around the suites.
- `gate.py --tests-only` and `--affected` (targeted_test): `default_owner(copies.source_owner(repo))`.
  `run_tests_only` reads `copies.source_owner` only when `request_owner("")` gives `OwnerNotGiven`:
  the caller set no owner variable and its process family names no owner, so a run from a terminal keeps `terminal`.
- `cloud_gate.service_command`: `owner_environment()` passes the owner variables to the systemd unit.
  A state-root staff caller started the work, so it passes its staff id in `BUILDLOG_OWNER_STAFF_ID`.
  Another family owner passes `CLAUDE_SESSION_NAME`.
- A gate test module: `test_module_environment` sets `CLAUDE_SESSION_NAME=gate module` when it inherits no owner.
- A test fixture: `verify_test_environment` and `gate_isolation_environment` always set `CLAUDE_SESSION_NAME=test fixture`.
  `gate_isolation_environment` also sets `CLAUDE_CODE_SESSION_ID`, `CODEX_THREAD_ID` and `BUILDLOG_OWNER_STAFF_ID` to empty.
  `verify_test_environment` copies only an allowed list of variables, and no owner variable is on it.
  So these fixtures never read the owner from the environment around them.
- A child process of `admission_test_support.caller_admission`: the fixture sets `CLAUDE_SESSION_NAME=test fixture`
  in its environment when that environment names no owner.
`verify.sh` and the cargo wrapper give no owner variable of their own.
Their requests take the owner from the caller variables, the process family, or the worktree owner record.
`default_owner(reference)` sets `CLAUDE_SESSION_NAME` to the staff name of the reference, or else to the reference.
When the reference is a staff unit or a worker of a unit, it also sets `BUILDLOG_UNIT` to that unit if `BUILDLOG_UNIT` is empty.
It never sets `BUILDLOG_OWNER_STAFF_ID`. `hold_enforcer.restart_entry` reads that variable for a run with no session.
A hold that stops the run sends a restart notice to that staff member when the hold ends.
The default owner did not start the run, so it must not get that notice.
It does nothing when an owner variable is already set, and it puts back the earlier values at the end.
`snapshot_codec.wait_line_name` names a waiting step for the client wait line, the service head label, and the Builds page "behind" text.
`owner_name`, `row_what` and `wait_line_name` are in `snapshot_codec`, because the admission client loads it at start and the AWS guest ships it.
It takes the owner from `owner_name`, with the staff labels when the caller has them, so a wait line and a row show the same owner.
It uses the folder name, and then the head name, only when `owner_name` finds no usable name.
When the stored name is empty, the Builds page shows the staff name or `unregistered session`.
The service, snapshot decoder, and Builds page use `resolve_queue_caller`.
The resolver uses the first usable name from the record, the reservation, and the staff snapshot.
It returns `unregistered session` when no candidate is usable.
A later read can replace this fallback with a usable name.
The shared name check rejects blank text, dashes, raw IDs, text with `$`, and the fallback label.
A staff unit uses its own name for priority. A worker uses its parent unit.
A showrunner keeps its caller name and has an empty unit.
A valid wait resets checker-failure time. Checker failures after a wait retry until `BUILDLOG_MEM_CHECK_RETRY_S`.
A first checker failure or explicit refusal stops at once. A launch deadline applies during transport and checker retries.
A zero launch limit refuses its first hold and accepts an immediate grant.
The leave hook runs during held waits and returns `LeftQueue` when another machine can start the step.

`detached_runs.py start` checks for a live twin before it applies `config/detached_runs.conf` `LIVE_RUN_LIMIT` to the worker.
The launcher accepts `--kind python-tests` before `--` and defaults to `detect`.
It records the kind and creates the scope `detached-<kind>-<run-id>.scope` when systemd is available.
The scope name omits the worker suffix from the run ID.
The Builds page reads the kind from the scope name.
For a `detect` scope or an unmarked scope, the page estimates memory use from live Python test processes and their descendants.
Orphan reaping uses the same runs-folder lock as start, including the `reap` verb and launcher reaping.
The reaper cannot finish a record while start publishes its supervisor PID.
Only running records with their matching supervisor hold a place.
An exclusive `<runs>/.start.lock` covers the checks, record creation, supervisor creation, and PID publication.
At the limit, exit 4 starts nothing and names each live run and label on stderr.
Wait for a run's result as the next message, or put more files in one run.
Exit 3 still means the caller is not a registered worker; only that exit permits attached execution.

`queue_place` uses the client in process. Its public call retains `before_check`.
The build queue has one way in. `queue_place.request_place` is the one requester, and `queue_place.release_place` is the one releaser.
`request_place` takes a `RefreshedRequest` (a step or program request, local or cloud), a `RefreshedLaunch` or a `ServiceEstimateRequest` (a CI cargo call).
`release_place` defers cleanup signals while it releases.
`queue_place.held_reservation` uses both. It waits for the grant, then releases it with the holder's status when the holder exits, also after an exception.
`queue_place`, the cloud route's `cloud_reservation` and `ci_cargo_front.py` use `held_reservation`.
`memory_gate.sh` runs `queue_place.py admit`, `launch` and `release` (`BUILDLOG_QUEUE_PLACE_SCRIPT`) for verify steps and BRP launches. These shell actions use the same two calls.
`admission_client.py` has no `admit`, `launch` or `release` action. It keeps the placement actions, `width` and `token-wait`.
A first refresh that fails maps to a checker refusal. A queue exit during it maps to `LeftQueue`, and an unreachable queue maps to a refusal.
`test_queue_place.OneQueueEntryTests` fails when code outside `request_place` and `release_place` calls the client's wait or release functions.
`release()` and `token_wait()` take explicit cleanup timing or the place's acquisition configuration.
`Admitted.cleanup_timing` supplies `CleanupTiming`; the shell retains it in `BUILDLOG_MEM_CLEANUP_TIMING`.
Stored `Admitted` replies without cleanup timing receive `PROTOCOL_CLEANUP_TIMING` before strict parsing.
Existing stored timing and valid shell timing retain their values.
The hook runs before each attempt and after a grant. An exception after a grant returns the reservation before program startup.
`popen` starts the program in its own scope, nested when the holder already runs in `builds.slice`.
Without a scope, it raises `QueuePlaceRefused` before the fork.
Only `BUILDLOG_SCOPE=0` enables process-tree sampling for tests.
`finish()` rereads `cgroup.procs` until empty before release. It sends TERM, then uses `cgroup.kill` after `program_stop_seconds`.
The scoped wrapper requires a published sidecar and a live holder before program execution.
`QueuePlace.popen` installs no signal handler.
It masks TERM, INT, and HUP until program publication and the `on_started` callback finish.
It then restores the caller's previous signal mask.
The program wrapper unblocks these signals before exec.
The program wrapper atomically publishes a `ProgramStart` record before execution.
The record contains the holder identity, program identity, and memory scope.
The wrapper checks the holder's PID and start time before and after publication.
A dead holder prevents program execution.
A later interruption leaves the complete record in the build log's `program-starts` directory.
The next start stops recorded scopes or program groups with dead holders before it requests admission.
It removes their records after their scopes or process trees become empty.
Normal cleanup removes the record after the program and its descendants stop.
Temporary queue-place folder names contain the holder's PID and start time.
The next start removes this account's folders after those exact holders end.
Folder allocation and removal use a signal mask and install no handler.
SIGTERM uses its default action and can leave cleanup for the next start or service read.
Python's default SIGINT raises `KeyboardInterrupt` and runs the existing cleanup blocks.
`defer_cleanup_signals` retains its signal mask during normal cleanup.
It uses `pthread_sigmask` and installs no handler.

### Startup, cut-over, and rollback

The system socket starts the system service as the user. The service runs outside `builds.slice` with `OOMScoreAdjust=-900`.
It sends `READY=1` after listener setup.
The main loop clears the outage claim once after `silence_seconds` of continuous operation.
It uses the last usable configuration and retries a failed clear on a later loop.
A shorter service start preserves the claim.
Source changes stop the service after the settle interval. A missing source also counts as a change.
Under systemd, the service then runs the new code in the same process with `execv`, and keeps the inherited socket.
It sends `RELOADING=1` before `execv` and `READY=1` when the new listener is ready, so the systemd restart delay does not apply.
The service logs the changed source paths before `execv`.
The replacement sends `READY=1`, then hashes the Python sources under `scripts` and logs the hash.
The hash includes sources that load only after startup.
A failed start of the new code exits, and `Restart=always` starts the service again.
A service started without `LISTEN_FDS` stops on a source change and does not run `execv`.
`down-alert` waits `silence_seconds` and claims `build-admission`.
It sends the outage message to the `builds` session first.
The message names the journal command and the restart command.
If the result is not `sent`, it sends an `alert.blocked` phone push.
The phone action is `systemctl reset-failed build-admission.socket build-admission.service && systemctl start build-admission.socket build-admission.service` on natedev.
It needs no sudo: a polkit rule in the nixos module `modules/linux/development.nix` permits only `reset-failed` and `start` on these two units.
The push clears when `admission_client.py view`, through `scripts/lib/py`, succeeds.
The sender releases the claim only when both sends return `failed`, `refused`, or `retry`.
Every other outcome preserves the delivered claim.
The sender prints each send outcome header to the journal.

`build_queue/head_stall.py` watches for a stalled queue head. `buildlog sample` runs it each minute on natedev.
It reads `queue.json` and the tail of `queue_times.jsonl`. It does not ask the service.
The watch uses `head_stall_alert_minutes` from `build_admission.conf` as the stall limit.
The head must wait at least that long. At least one step must hold memory.
The last admission or step start with a memory need above zero must also be at least that old.
Merge gates and targeted tests start with a memory need of zero.
The watch skips snapshots older than `silence_seconds`. `down-alert` covers a silent service.

The watch sends one alert per stall to each of the `builds` and `bob` sessions.
The alert names the head and lists each holder's time since admission, memory promise, and memory use.
It adds a note when an incomplete sccache scan can make the use values too low.
The stall key is the most recent admission or step start with a memory need above zero.
A head change keeps the same key. A new admission or step start ends the stall.
The sender uses the stall key to keep one queued copy per recipient.
The watch accepts `SENT`, `SKIPPED`, `QUEUED`, and `KEPT` as delivery outcomes.
After each accepted send, the watch records the recipient and stall key in `~/.local/state/buildlog/head_stall.json`.
An old shared key counts for `builds` only.
If a state write fails, the watch continues to the next recipient and reports one write error after the sends.
If the stall continues, the watch retries each unaccepted send the next minute.

The 2026-11-17 stall held the head from 00:36 to 03:46 EST. No alert reached anyone.

The builds showrunner owns cut-over. Workers do not change live units or admission state.

1. Stop and disable the socket and service before the prerequisite rebuild.
2. Remove the old `/var/lib/build-admission` state after its service drain succeeds.
3. Hold natedev and require quiet builds plus `admission_service.py drain-check` reporting `drained`.
4. Merge the cut-over alone.
5. Enable only the socket and rebuild the system configuration.
6. Check the service command, live view, cgroup, and OOM score.
7. Release the hold.

`drain-check` counts live imported waiters, reservations, and processes that retain old admission functions.
`drain-check --service` counts live service waiters, reservations, and clients through the same process list.
Both commands only read state. A nonempty count refuses the drain.
`state-check` reads `state.json` with the same strict reader as `serve`, from the same directory: `--state-dir`, which defaults to the live admission directory `serve` uses. It exits 0 when the file reads or is absent, and 2 with the error otherwise.
Promotion runs it from the target tree, so code that cannot read the live state does not reach main.

1. Hold natedev before rollback.
2. Require quiet builds and `drain-check --service` reporting `drained`.
3. Stop and disable both units before the rollback rebuild.
4. Run `export-priority` to preserve the active priority and remove the drained `admission/state.json`.
5. Revert the cut-over only after the export succeeds.
6. Release the hold.

A bare revert can hide live service charges from the restored checker. Never revert before the drain.
An export retry preserves the exported priority when `state.json` is absent.
The next cut-over imports the polling checker's current priority because the retired state file is absent.

### Cargo wrapper (natedev only)

`scripts/build_hold/cargo` sends building cargo commands from shells, scripts and TUIs to the build queue.
This includes `cargo mend`, which builds the workspace.
The wrapper uses the first other cargo on PATH and keeps its arguments and exit status.
Non-building commands such as `metadata`, `fmt` and `tree` run immediately in Bash, with no Python and no queue request.
`test_cargo_wrapper.test_non_building_commands_start_with_no_python_and_no_queue_request` proves this.
A call already in `builds.slice` runs at once, unless it runs under `builds-detached.slice`.
Such a call comes from a queued step or an editor scope, so it has no second queue place.
A queued step of a detached run also runs its calls at once.
The wrapper finds such a step when its cgroup folder equals `BUILDLOG_STEP_SCOPE`, which the step scope exports.
Any other call under `builds-detached.slice` follows the same hold and queue path as a call from a shell.
These building calls check `hold_applies.py` when the hold directory has an entry.
An applicable hold refuses the command.
Unreadable state prints a note and allows the command.
An empty hold directory starts no hold check.
The wrapper then runs `scripts/build_queue/queue_place.py -- <cargo> <arguments>`.
The queue place waits for admission, starts cargo in its own `builds.slice` scope and releases the place when cargo exits.
A complete cargo command sizes from its step history, as a `verify.sh` step does; `program_first_peak_gib.cargo` is the first peak.
Without a user bus or `systemd-run`, cargo runs directly.

Install on natedev from the repo root with `scripts/build_hold/install_cargo_wrapper.sh`. It links `~/.local/bin/cargo` to the wrapper and refuses to replace another file. `~/.local/bin` must precede `~/.cargo/bin` on PATH. On macOS, the installer makes no link and the wrapper calls cargo directly. Tests use `BUILD_HOLD_DIR`, `CARGO_WRAPPER_CGROUP_FILE` and `XDG_RUNTIME_DIR` to select temporary state.

### BRP launches

`scripts/hooks/pre-tool-use-brp-launch-gate.sh` is a `PreToolUse` hook on `mcp__brp__brp_launch` (`settings.json`, timeout 960 s). It sets `CLAUDE_CODE_SESSION_ID` from the hook input, clears the call id and asks `build_targets.py check-local` first. A launch from the session whose hold file is the only hold in force is admitted. A hold or unreadable hold state prints a deny answer with its reason and exits 0 without memory admission; the session launches again after release. An allowed launch runs the memory gate. A launch's build runs in the session's own scope, not `builds.slice`. A launch after release enters the memory gate without another release message.

### Memory kills in `verify.sh`

`run()` wraps `run_once`. A failed step is a memory kill when its output shows `SIGKILL|SIGTERM|signal: (9|15)` and the journal shows, inside the step's window, earlyoom's `sending SIG(TERM|KILL) to process` (`journalctl -u earlyoom`) or the kernel's `Memory cgroup out of memory` / `Out of memory: Killed` (`journalctl -k`). The query lives in `memory_kill_in_journal` so tests can stub `journalctl`. A memory kill is never the tree's failure: the step re-runs once (`killed for memory: <step>; retrying once`), and a second kill exits 137 with `killed for memory twice: <step>`. Each kill appends a byte to `MEM_KILL_FILE`; the second creates `<file>.stopped`. `note_event` passes `BUILDLOG_MEM_KILLS` and `BUILDLOG_MEM_KILL_STOPPED` to `record.py call`.

`verify.sh` also times the `cargo` board-token acquisition and passes it as `BUILDLOG_TOKEN_WAIT_S`. The clock is Bash's `$SECONDS`, or the integer in `UNIT_TOKEN_CLOCK_FILE` when set, so a test advances a file instead of sleeping. A wait that times out exits 1 before any call is recorded, so its time is in no row.

`/build:bump` resolves the waiting step's recorded unit before it checks roster labels.
The no-unit display placeholder supplies no unit.
Unknown units keep the saved priority and return exit 3 with a refusal on stdout.
The Builds page shows the saved priority order and reasons.

Durable `Waiter` rows keep their join and token clocks. `RankedWaiter.order` holds `PriorityUnit` or `NormalOrder` only in the resolved queue. Priority transitions decode to `SettledPriority` or `PendingPriorityTransition`; a pending transition keeps its id and saved month-log offset through retries. One cleanup guard isolates both a failed token-wait intent and failed dead-owner reservation finalization. Each failure logs its record path and keeps the record for the next poll; other cleanup and admission continue.

### Cargo token reclaim (`scripts/direct/board.sh`)

A local compiling attempt waits for placement and memory without the board's `cargo` token.
A token from an earlier step of the same call is released before the next placement.
After a local grant, `verify.sh` takes the token, then runs cargo. The grant keeps its reservation during that token wait.
So a memory wait no longer holds the cargo token. The token wait after a grant lasts only while a peer of the same unit runs cargo.
A step placed on the Mac or an SSH target never takes the natedev token.
`acquire_token()` waits in the existing FIFO queue and reuses a token that this call already holds.
When the token wait after a grant ends without the token, the call cancels its placement or releases its reservation.
A natedev hold that starts during that wait ends it: the call releases its reservation and places the step again.
The execution guard still checks the token owner before cargo starts.
`unreachable_limit_seconds` limits missing-socket and refused-connection retries to 60 seconds by default.
The client reports the retry interval once when a connection fails.
A successful connection resets that interval.
At the limit, the client exits with status 2, and verify starts no cargo.

Cancellation, a hold, and departure for the Mac release the token.
A later local attempt takes the token again after its next grant.
Each `run()` path releases tokens that it acquired, as required by verify Phase 36.
Noncompiling local steps retain their existing token path and request no memory.
The wait log names the holder and its PID, including holder files without a final newline.
It says `this call holds it` when the recorded PID equals the call's PID.

Old shells can still send `admit`, `token-wait`, and a second `admit` with `BUILDLOG_MEM_TOKEN_BACK_AT`.
The service keeps this compatibility path and records its token gap.
New shells generate neither the second request nor a token gap.

A hold releases the token until the hold ends.
A call that waits again keeps its original FIFO position through its `.again` waiter file.
The hold wait and token wait retain separate history records.
`TOKEN_WAIT_FILE` carries a step's token wait to the call record.
`TOKEN_WAIT_FILE.given_back` records a subshell release for its parent.
 An INT, TERM or HUP during the token wait stops the waiting acquire and gives back a token it took in the last instant, and with `--pid N` `board.sh acquire` itself exits 4 (`gone <res>: pid N ended while it waited`) once N is gone. A step whose call is gone stops its token wait (`token_wait_orphaned`): each pass checks that the call's pid (`$$`) still runs, an acquire that exits 4 ends the wait too, and the step prints `verify.sh: the call that started this step is gone, so the step stops waiting for the cargo token.` and exits 1. On 2026-10-20 a test killed a call's group and left its step, which runs in a group of its own (`run()`'s `set -m`), trying again for 45 h with 15 GiB of deleted output still open; the test's cleanup now also ends every process in the call's session. A release moves the lock's `reclaimed_holders` lines whose holder still runs to `locks/<res>.reclaimed`, and the next acquire moves them back into its lock, so a reclaimed holder's late release still exits 3 after the token changed hands again. `board.sh acquire --pid N` (a positive integer, else exit 2) records N in `locks/<res>.d/holder_pid`. A waiter treats the token as stale when that process is gone, checked first, or when its hold has expired. Gone means `ps -p N -o stat=` exits 1 with empty output or reports a zombie; anything else, a failure of `ps` included, counts as alive, and a lock with no or a malformed `holder_pid` waits for expiry. So a `verify.sh` killed before its EXIT trap frees the token within one 3 s poll, posting `token cargo reclaimed from <holder>: holder pid <N> is gone`, while a manual holder is never reclaimed early. A newer call from the same worker, with the same verb and arguments in the same worktree (a lint key leaves out its package), ends an older one that still waits for its first token: each waiting call keeps `token_waiters/<slot>.<key>.<start>.<pid>` on the board, and a newer call sends SIGUSR1 to each older one whose pid still runs `verify.sh`. The older call gives back a token its acquire took in the last instant, prints `verify.sh: superseded by a newer <verb> call`, records outcome `superseded` and exits 76 (`VERIFY_SUPERSEDED_STATUS`); `detached_runs.py` delivers that as `Result: SUPERSEDED (exit 76)`, not a failure, also when the run has a done message. A call whose acquire already won runs to the end, and a SIGUSR1 after the first wait does nothing. On 2026-10-21 one worker queued 27 calls this way, most on trees it had already left, and its peers waited up to 20 h for the token. Reclaiming a dead holder first SIGKILLs the step process group it recorded in `locks/<res>.d/owned` and fails closed (acquire exits 1, token left in place) when that group cannot be inspected or stopped; an expired hold whose holder is alive is reclaimed without touching its build. Reclaim and release use an exclusive flock on `locks/<res>.guard`. One perl call locks descriptor 9 because the Mac has no `flock(1)`. The shell retains the lock after perl exits. The kernel releases the lock when the holder dies. A reclaimer reads the lock again while it holds the guard. Thus, two waiters cannot both reclaim the lock. A release cannot remove a lock that a reclaimer just made. Reclaim never waits for the guard; release retries for up to 10 s, then exits 1 (`board.sh: cannot guard <res> release`).

### Build log: records and schema

Index `SCHEMA_VERSION = 33` (`scripts/buildlog/index.py`). `open_for_update` rebuilds the index on a version mismatch.
Memory and coordination columns:

| Table | Column | Meaning |
| --- | --- | --- |
| `steps` | `mem_wait_s` | seconds the gate held the step (`BUILDLOG_MEM_WAIT_S`) |
| `steps` | `slice` | `builds` (scope ran), `fallback` (scope failed, ran without one), `none` (no scope tried) |
| `calls` | `token_wait_s` | cargo-token acquisition seconds; 0 when absent or not a whole number |
| `calls` | `mem_kills`, `mem_kill_stopped` | memory-killed attempts; 1 when a step was killed twice |
| `samples` | `builds_anon_bytes`, `ci_anon_bytes` | the `anon` line of each slice's `memory.stat` (process memory); NULL when absent |
| `samples` | `sccache_in_service` | 1 when `builds.slice/sccache.service/cgroup.procs` holds a process, 0 when empty, NULL without `builds.slice` |
| `samples` | `zram` | JSON zram storage at the sample time; NULL in older rows |
| `memory_snapshots` | `src, host, at, slices, zram` | JSON slice counters and zram, indexed on `at` |
| `ci_jobs` | `run_state` | `JobRunState.RAN / SKIPPED / CARRIED_OVER` |

`steps.wait_s` keeps metadata and pass-record lookup time plus the ordinary build wait; `mem_wait_s` is memory admission. `calls.hold_wait_s` records held-target waiting separately, including each declined Mac retry, and `calls.target` names where the call ended. The Waiting table keeps `Held target` apart from `Memory admission`.

### Slice snapshots

`scripts/buildlog/memory.py` supplies the slice snapshots.
Each `buildlog sample` tick writes one sample row with slices and zram.
`memory_snapshots` holds standalone snapshots and the zram-only tick rows of older logs, so the memory window reads both tables.
The standalone `buildlog snapshot` row keeps slices and zram.
Each slice (`builds` or `ci`) stores its events, peaks, limits, and memory pressure total.
An absent slice has `state: absent`. An unreadable pressure total has the value `unavailable`.
The zram reading stores the data and compressed bytes from `mm_stat`.
It takes the nearest slices value, then the nearest zram value, each from both tables.
With no slices in range, the result is unavailable.
Snapshot rows have priority on equal distances.
`BUILDLOG_BUILDS_CGROUP`, `BUILDLOG_CI_CGROUP`, and `BUILDLOG_ZRAM` select fixture paths for tests.

**CI jobs.** A job is `carried_over` when its attempt is above 1 and `started_at` precedes `created_at`, else `skipped` when its conclusion is `skipped`, else `ran`. Only `ran` rows store `duration_s` and `queued_s`; `queued_s` is `started_at - created_at` when both parse and the result is zero or more, else NULL. `elapsed_seconds(start, end) -> float | None` is unrounded; `seconds_between` rounds to 3 places. `ci_job_days` excludes `carried_over` and keys days by the run attempt's `started_at`.

**CI poll freshness** (`scripts/buildlog/ci.py`). Each poll that reached GitHub writes `<root>/ci/polled.json` `{repo: {polled_at, complete}}` atomically; a cold-credential skip or `GhError` writes nothing. `read_poll_state(repo) -> CompletedPoll(polled_at) | CappedPoll(polled_at) | NeverPolled()`; a malformed or zone-less stamp is `NeverPolled`.

**BRP launches** (`scripts/buildlog/launches.py`). `collect() -> LaunchCounts` turns each `brp_launch` result in `~/.claude/projects/*/*.jsonl` (`BUILDLOG_TRANSCRIPTS` replaces the root) into a `build` step with caller `brp-launch`: `ended_at` = `metadata.launch_timestamp`, `started_at` = that minus the launch duration, argv `cargo build --workspace --bin|--example <target> [--release]` (`--bin` only when `metadata.launched_as` is `app`), no memory fields. It reads `mcpMeta.structuredContent`, list `tool_result` items and `<task-notification><result>JSON</result>` strings, and skips any line lacking `brp_launch` or one of those markers before parsing. `launch_record(line, transcript, *, backfill=False) -> RecordedLaunch | LaunchOutcome` takes the location from `parameters.path`, else `metadata.working_directory`, else `LaunchOutcome.NO_LOCATION`; repo and worktree come only from `record.git_facts(location)`. Each launch is `launch-<sha256(session:timestamp)>.jsonl`, written only when absent (temporary file, hard link). `launches_state.json` holds each transcript's offset and marks; an entry without `reread_complete` is re-read once from the start, launches before its saved offset recorded with `branch` and `sha` null. `LaunchCounts.summary()` prints on stdout for `buildlog launches` and on stderr for `buildlog report`. `day_report` collects before `index.update()`.

### Build log: commands and report

| Command | Output |
| --- | --- |
| `buildlog snapshot` | writes one snapshot |
| `buildlog memory START END` | window report; ISO instants with offsets, else `usage: buildlog memory START END (ISO 8601 instants with offsets)`, exit 2 |
| `buildlog launches` | collects; prints the counts line |
| `buildlog sync pause <why>` / `resume` | see Mac sync below; other arguments: `usage: buildlog sync [pause <why> \| resume]`, exit 2 |

**Window report** (`memory.window_report`), host-scoped (`store.host_name()`), times in PDT:

```
Memory, <date time PDT> to <date time PDT>
earlyoom kills: N            (then one "<HH:MM PDT> <comm> pid <pid>" line each)
kernel OOM kills: N (builds B, CI C, elsewhere E)
builds: high +N, max +N, oom_kill +N; stall +N.N s; peak X GiB, limit Y GiB; swap peak ...
CI: ...
memory waits: N steps, total T, longest L, K reached the 15-min limit | memory waits: none
sccache in its service: every sampled minute | sccache outside its service: N min — <runs> | sccache service: unavailable
unsliced steps: N fell back to a plain run, M never tried a scope | unsliced steps: none
memory kills: N calls passed after a re-run, M failed later, K stopped after a step was killed twice | memory kills: none
zram: X GiB stored in Y GiB (R:1) | zram: unavailable
```

Slice lines use the snapshots nearest each edge within `SNAPSHOT_DISTANCE_S` (120 s), else `<slice>: no snapshot within 2 min of <time>`. A journal error or nonzero exit prints `journal unavailable`, never zero kills. A peak that did not rise in the window reads `at most X GiB (no new high in the window)`. A counter that falls in the window is a reset: `<slice>: reset in the window; since the reset ...`. Edges round up to the next millisecond (`stored_bound`) to match stored records.

**Daily report** (`buildlog report [day]`, `scripts/buildlog/report.py`). Every step-based section counts natedev's steps only (`host = 'natedev'`); `### Mac` holds the Mac's (`docs/as-built/mac-test-offload.md`). Order: `## Builds, <day>`; `### Waiting`; `### Rebuilds` (where compile time goes; `docs/as-built/build-followups.md`, Rebuilds section); one section per kind; `### Memory pressure`; `### CPU and disk pressure` (`docs/as-built/buildlog-memory-stalls.md`); agent calls; test builds; `### Power profile` (`docs/as-built/build-targets.md`); Tests by target; `### Mac`; port-lint; `### CI`; `### Tests per edit`; Disk; summaries; one-line totals and notes.

- `### Waiting`: a `Wait | Longest | Over 5 min | Waited | Total | Worst` table, always present, in this order: `Build-folder turn, behind another worker`, `Build-folder turn, behind its own call`, `Held target`, `Memory admission`, `CI queue`. Held-target seconds come from `calls.hold_wait_s`; memory seconds from `steps.mem_wait_s`. `Longest` names who and its time; `Over 5 min` counts above 300 s; `Waited` is the count out of the day's population; `Total` is worker-hours or job-hours; `Worst` gives the top three. Each row uses `wait_row`; an empty kind reads `none`.
  - **Build-folder split.** `build_folder_waits(connection, day) -> (another_waits, own_waits)` returns two `ReportedWait` lists (`tuple[float, str, str, str]`: duration, owner, name, started_at), one entry per call in each, with the call's part of the wait as the duration and the worktree as owner and name. It reads the day's `tool = 'verify.sh'` calls and the token holders: verify.sh calls with a `unit_session` and an `ended_at` on the day or later. `token_holders()` turns each into `TokenHolder(call_id, unit_session, worker, starts_at, ends_at)` over `[started_at + wait_s, ended_at]`, grouped by session so each call scans only its own session's holders.
  - The cargo token is held per worker session, so only calls of the same `unit_session` can cause a token wait. A positive `token_wait_s` on a call with a session goes through `attribute_token_wait(call_id, unit_session, worker, starts_at, wait_s, token_wait_s, holders) -> TokenWaitAttribution(behind_another_worker_s, behind_own_call_s)`; a call with no `unit_session` puts its whole wait in the another-worker row.
  - `attribute_token_wait` takes the wait interval `[started_at + wait_s - token_wait_s, started_at + wait_s]` and splits it at the boundaries of the overlapping same-session holders other than the call itself. A segment is own only when every holder covering it has the call's worker; every other segment, uncovered included, counts as another worker. Own is capped at `token_wait_s`; another worker is the remainder.
  - A NULL worker reads `(unknown worker)`. Session calls with no worker never wait but can hold the token, and a worker waiting behind one counts as another worker.
- `### Memory pressure` always has its heading. It carries `memory waits: N steps, total T, longest L (<caller>)` or `memory waits: none`, the three instrument lines (sccache, unsliced steps, memory kills; natedev's samples, steps and calls only), the stall table, and `60 s samples: peak used memory X, peak swap Y; peak builds B, peak CI C (process memory); machine stall: ...` (`unavailable` per slice without samples).
- A `p95` follows every average: the kind tables (`COMMON_HEAD = Runs, Failed, Avg, p95, Range`; `COMMON_SQL` fetches `group_concat(duration_s)`), each `AverageColumn` (`<title> p95`), the summaries, Tests per edit (`p95 to next green`) and the CI queue clause. `nearest_rank(values, percent)` uses sorted index `(percent*n+99)//100-1` and also serves the Test builds p75 and the Rebuilds package p50 and p95.
- `### CI`: head `Workflow, Runs, Failed, Cancelled, Avg, p95, Range`; the summary names `<n> cancelled` only when nonzero. The queue clause is `jobs queued <avg> on average, p95 <p95>`, plus `, <N> without a known queue time left out` when any, or `no job has a known queue time` (`(<N> left out)`). Days key on the run attempt's `started_at`. Both CI summary paths end with `ci_freshness(day)`: `; CI never polled`, or `; CI recorded through <time>`, plus `, no poll since` when older than `min(now, day end) - CI_STALE_AFTER_S` (2 h) and `, the last poll was capped` for a capped poll.
- `brp-launch` steps show as `example launches (brp)`; launches from temporary folders group under their worktree, not as scratch (`GROUP_AS_SCRATCH`).
- The Mac note starts `Mac: sync paused since <time> (<why>).` while paused.

### Mac sync pause (`scripts/buildlog/sync.py`)

The hourly task exchanges records with every configured SSH peer.
After a completed Linux offload run, natedev releases the slot claim before it calls `sync_target(target)`.
This exchange contacts only that target and preserves its configured host paths, including `aws-build/`.
It skips a paused sync and never waits for the sync lock.
It uses the configured connection, reach, and rsync timeouts.
A failed exchange prints one line and preserves the step result for the caller.
The hourly task can retry the exchange.

`buildlog sync pause <why>` writes `<root>/sync_paused.json` `{since, why}`; `resume` removes it. `sync()` holds an flock on `<root>/sync.lock` for its whole run; paused, it contacts nothing, prints `buildlog sync: paused since <time> (<why>); buildlog sync resume ends it`, returns 0 and leaves `sync.json` alone. `pause` takes the same lock (printing `buildlog sync: waiting for the running sync to finish`), so once it returns nothing contacts the Mac. `hourly` still polls CI while paused. `read_pause -> SyncPaused(since, why) | SyncRunning()`; `read_status -> SyncStatus(at, peer, ok, last_good: LastSynced | NeverSynced) | NeverSynced`. The reach check runs `mkdir -p <REMOTE_ROOT>; echo "rc=$?"` and judges the printed `rc=`; nonzero or missing (`reported rc=<n>` / `no rc= line in command output`) writes `sync.json` `ok: false` and exits 1. ssh exit 255 is a peer that did not answer (one line, exit 0). `rsync` keeps its exit-status checks.

### Build hold (`scripts/build_hold/build_hold.py`, `commands/build/hold.md`)

**Holder files.** `BUILD_HOLD_DIR` defaults to `~/.local/state/build-hold/`.
Each holder has one file with one JSON line: `{holder, since, for, release_eta, keep}`.
`release_eta` stores the release instant; old `"unknown"` values remain readable.
`keep` holds PID/start-time pairs and defaults to empty for old files.
Installed scripts refuse directory overrides or a changed `HOME`.
Unset the override or use a shell with the user's home.
The shell guard also refuses a read-only `HOME`.
Legacy free text remains readable.
An unreadable or malformed regular file remains a hold.
The types include `HolderIdentity(key, name)`, `HoldState = NoHolders | ActiveHolders`, and `Holder(name, since, purpose, release, key, keep)`.
`ReleaseEta = KnownReleaseEta | UnknownReleaseEta` describes the release time.
`ReleaseRequest = NoReleaseEta | ClockReleaseEta | DatedReleaseEta` describes the request.
`write_hold` and `start_hold` take the resolved identity.
Holder files supply state to status and Rust release checks.
`natedev_holds(directory) -> NatedevHolds(in_force)` reads holder files once and never reads the cycle.
Every present holder file remains in force, including when the cycle has `released_at` or `release_started_at`.
There is no releasing state.
Only removal ends the holder's hold.

**Drain.** `build_hold.py drain --for '<why>' --release-eta HH:MM --zone IANA` keeps the natedev runs that work until they finish.
It captures outermost gates that work, CI `Runner.Worker` processes, and builds that the enforcer could stop.
A gate works when it has a cargo child, an admission reservation, or a Mac or AWS claim.
A live step record also counts as a claim when the call cannot run on natedev, or waits for or runs on the Mac or AWS.
`remote_step_callers` names these gates in `LiveClaims.routed`, and clear and the enforcer never stop them.
A drain does not keep a routed gate. A later natedev step of that call waits for the release, unless its run took natedev.
An unreadable step record of a live writer makes the claims unreadable.
On 2026-11-15 a natedev hold stopped two gates whose builds waited for or ran on the Mac.
The drain also keeps each run that natedev took, also between its steps (`hold_enforcer.taken_runs`).
A kept run is its run controller process. A later step under that process is kept.
A taken run is one of these:
- the owner of a verify run slot, which holds natedev from the first local grant to the end of the outer verify call;
- the run of a live local reservation;
- the owner of a live local reservation with no run.
The drain keeps the run and the outermost working process of its step, so a step stays kept when its run controller exits.
A run that only waits has no slot and no reservation, so the drain does not keep it.
A gate that only waits, under no taken run, is not kept. It waits for the release like new work, and the drain does not wait for it.
Work under no taken run is kept by its own identity.
Unreadable claims or admission state keep every outermost gate.
With unreadable admission state, the drain reads each run from `BUILDLOG_RUN` (`pid:start`) of the working process or an ancestor, and keeps it when that run is a live ancestor.
The unit in `max_runs.exempt_unit` gets no verify run slot, so the drain keeps its run only while a step has a local reservation.
Between its steps the drain does not keep it. A slot for exempt runs is a queue change.
It publishes the hold with the first keep list and records a boot-relative start-tick bound immediately before publication.
A second scan adds only identities that started before that bound.
The command merges those identities under the release lock before starting the enforcer.
If release removes the holder file during discovery, the command ends without restoring the file.
Each keep entry has a PID and a start time.
The enforcer and the admission service use this same keep list for descendants.
The lineage reader uses process identity files without reading private environments or command lines.
New natedev runs wait for explicit release.
The command stops no process and adds no stored state field.
It accepts `--keep`, requires `--release-eta` and `--zone`, and refuses Mac and SSH targets.
Each 60 seconds, it prints `draining: N runs remain`.
Completion prints `drained; natedev stays held until release` and returns 0.
Ctrl-C returns 130 and retains the hold.
Status derives `draining (N runs left)` or `held` from matching live keep entries.
Status and the enforcer report list only live keep entries.
A reused PID does not count when its start time differs.
Status names each queued or running local CI placement and its state start time from existing admission records.
On natedev, each admission pass applies the same hold and lineage check to every local step waiter (`AdmitRequest`), CI requests included.
App launches (`LaunchRequest`) are not held; the enforcer does not stop them either.
The check uses the waiter's process lineage and the session that owns its run, so the holder's own steps and kept runs still start.
The client sends that owning session (`owning_session()`) in `AdmitRequest.session`; a worker in the holder's run is not held.
A request from an older client has `SessionNotSent` and falls back to the session in its queue row.
The service finds the own hold key once for each session in a pass.
An unreadable staff record gives no own hold key for that pass, so the step waits.
A waiter that joined before the hold is held too; the check runs at each pass, not only when the waiter joins.
A held waiter keeps its place, gets a `NatedevHeld` wait and starts at the release in its order.
A held waiter claims no between-steps slot and gives back a slot it claimed, so that slot can expire during the hold.
View and explain replies between passes show the holds of the last pass.
Only an unreadable hold state gives a legacy CI request `ReadingUnavailable`, which the client refuses after its retry time.
Other local waiters are not held while the hold state is unreadable; the enforcer stays their guard.
The CI client that nixos pins (bbc8803f4) does not know `NatedevHeld`.
`snapshot_codec` writes it as `BehindHead` with a `_natedev_hold` field, and current readers restore it.
GPU slot waits use the same shape with a `_gpu_slot` field.
Repository run limits use `max_runs.<machine>.<repository>` in `build_admission.conf`.
An absent key sets no repository run limit.
A value of zero holds new runs on that machine.
`max_runs.natedev.hana` permits two concurrent Hana Verify runs on natedev.
`max_runs.natedev.cargo-liner` permits one cargo-liner Verify run at a time on natedev.
Verify asks for placement first and takes no slot before it.
A Verify run takes its natedev repository slot with its first local memory grant, in the same admission decision.
The slot belongs to the outer Verify process.
If the caller returns all local grants before any step starts, the service releases the slot.
After a local step starts, the run keeps its natedev slot until the outer Verify process exits.
The run keeps the slot between steps and during later Mac or AWS steps.
Later local steps of the run reuse the slot.
A child Verify uses a live ancestor Verify's slot for the same repository.
The service verifies each process in that chain by PID, start time, and parent PID.
A run that never receives a local grant takes no natedev slot.
The exempt unit takes no natedev slot.
A run that waits only for its repository limit holds back no step of another repository.
The service saves slots with their local grant and restores the owner watches after restart.
Local `gate` and `test-affected` runs count while they hold a reservation.
`max_runs.natedev.claude_commands` permits two concurrent runs and applies this rule to the repository's `gate` and `test-affected` steps.
The admission service uses the Git repository name when a gate profile supplies an absolute path.
The unit in `max_runs.exempt_unit` can start above the limit. Its gate reservations count toward the limit.
The Builds page shows the count, limit, repository, and machine for a run-limit wait, and each machine the run can use.
The snapshot codec writes the gate wait as `BehindHead` with a `_run_limit` field, and current readers restore it.
An existing CI reservation continues when the enforcer cannot stop its account.
No process permission changes support these checks.
**Clear.** `build_hold.py clear --for '<why>' --release-eta HH:MM --zone IANA` stops existing queued gates and running work outside the hold's allow rules.
It retains explicit keep entries and their children.
The command leaves the target held until release.
`clear` and `drain` accept an optional positional target before their options.
`release [target]` uses the same target form.
The shared `default_target=natedev` key selects the target when the word is absent.
Only natedev supports these commands.
Mac, AWS, and `all` return exit 2 with `<command> <target> is not supported yet`.
No unsupported command changes any target.
Use `/build:mac block <why>` for a Mac hold.
Use `/build:mac unblock` for its release.

### Step state publication

`step_record.py write --report` writes the JSON record once for each verify call.
It prints the clock offset in microseconds, the JSON path, and the sibling state path.
`initialize_step_record` saves those values and the original JSON text.
Bash uses `cmp` to compare the JSON bytes with the saved bytes before each state publication.
A missing, damaged, or changed record refuses the step with exit 2.

`publish_step_state` writes a state word and an optional target to `.step-<call id>.state-$RANDOM` beside the record.
Bash replaces `<call id>.state` with `mv` in the same directory.
It starts no Python process and performs no fsync for this temporary call state.
The cleanup removes the JSON record, the state file, and this call’s `.step-<call id>.state-*` files, in that order.
The `state` and `remove` Python CLI verbs no longer exist.

`step_record.read` opens the state file before it reads the JSON record.
It checks the writer process before it decodes the record, and keeps the state file open until it returns.
The writer is the record's `pid` and `proc_start`.
When the JSON is damaged, the writer is the pid in the call id (`v<UTC stamp>-<pid>-<random>`).
An ended writer, or a pid with another start time, returns `StaleRecord`, even when the record is damaged.
So a damaged record of a killed gate does not make the hold claims unreadable for good.
Without a state file, the reader keeps the original row.
The open state file supplies the UTC update time from its modification time.
An unknown state word returns `UnreadableRecord` through the existing validation path.
`on_mac` selects `mac`, `on_natedev` selects `natedev`, and `on_ssh` selects its supplied target.
Other state words clear the selected target.
Readers continue to find records through `*.json` only.

A tree-writing step publishes `writes_tree <state>` with an optional target for its whole lifetime.
The reader applies `natedev_only` with the reason `step writes the tree`, then joins the state and target normally.
The state remains visible so the hold enforcer can stop compiling steps in `on_natedev`.
A bare `writes_tree` has no state word and returns `UnreadableRecord`.
After the step, `run()` publishes `local_noncompiling` to restore the call's original decision.
The token boundary and the per-step release remain unchanged.
After a crash, readers ignore the record when its process identity is stale.

**Hold enforcer.** A holder file always means a hold.
Cycle release fields grant no permission.
`hold_enforcer.py run` checks once each second.
It reads the cheap process table once.
It then reads environments for this user's processes.
It reads arguments and working folders for candidates, their ancestors, and processes with call ids.

A process with `BUILDLOG_CALL_ID` uses `step_record.read`.
Live states `on_mac`, `waiting_hold`, `waiting_mac`, and `local_noncompiling` continue.
Children in `on_natedev` stop during another holder's hold.
The live record's controller PID and start time always continue, so verify can route the step again.
A missing, stale, or unreadable record grants no permission.
Only processes without a call id use command-name rules.

The enforcer matches arguments and executable basenames.
This includes absolute Rust toolchain, cargo bin, and Nix paths under direnv, systemd-run, or a Codex sandbox.
Matches include building cargo verbs from the shared set in `hold_applies.py`, nextest, compilers, clippy-driver, cargo-tile-real, and session-owned rust-analyzer.
They also include Python unittest, pytest, `test_*.py`, basedpyright, and shells that run `test_*.sh`.
Mac coordination scripts, version probes, cargo lookup verbs, and basedpyright-langserver continue.

The sole holder's runs continue.
Its `--keep <pid>` list and descendants continue after PID and start-time checks.
User terminal and desktop app runs continue without agent ancestors or session markers.
Shared sccache-server work, the enforcer, and its ancestors continue.
A run from before `since` has no special permission.

Every other run receives the holder and reason on stderr when possible.
The enforcer sends SIGTERM to the root first, then its descendants.
After two seconds, it sends SIGKILL to survivors.
It checks the start time before each signal.
A route state change does not cancel a pending kill for an identity that the enforcer already stopped.
New Mac children stay protected.
A release, sole-holder ownership, or an explicit keep still cancels that kill.

Each stopped root writes a permanent `hold_build` row through the shared audit writer.
Schema 21 adds `action=audit|stopped`; readers treat old rows as audit rows.
The row carries the root's `call_id` from `BUILDLOG_CALL_ID`, or an empty value.
Verify can thus find a stop after the hold ends.
Both stop paths register the owning session before the first signal.
They also register the nearest command agent when its recipient key differs from the owner's session id.
`run_agent(process, ancestors)` returns `RunAgent(session_id)` or `NoRunAgent`.
It checks the command first, then each ancestor in order.
At each process, `CODEX_THREAD_ID` gives the key `codex:<thread>` before `CLAUDE_CODE_SESSION_ID` gives a Claude session key.
The cycle keeps both recipients without adding a field.
The enforcer does not signal `verify.sh`, `targeted_test.py`, or `merge_gate.py` without a call id.
Each waits at its own hold gate.
With a call id, the record decides.
Verify routes eligible steps to the Mac and waits for other steps.
Their running natedev test and type-check children still stop.
They write stopped rows with the PID and start time.
The merge gate reads those rows and reruns the stopped modules after release.
No run pauses: cargo must release `/dev/steve` slots and locks, and wall-clock limits continue.

`clear` starts the user unit `build-hold-enforcer` with absolute paths, or keeps an active unit.
Without systemd-run or a user bus, it starts a detached process.
`BUILD_HOLD_ENFORCER_DIR` holds the singleton lock.
No paused record or thaw exists.
`run` exits when no holder file remains.
`release_overdue(release, now)` decides whether a known release time has passed.
Status shows `release overdue since HH:MM <zone>` for that hold.
The hold status and holder text add `, overdue` after the ETA.
The enforcer sends one overdue message through `send_holder_message`, which also serves `tell_wait_holder`.
The message names the purpose, release time, continued hold, and commands to release or set a later ETA.
An accepted send writes an atomic marker under the enforcer's `overdue-notices/` folder.
The marker identifies the holder, hold start, and release instant across restarts.
Failed sends retry after at least 60 seconds and use `hold_sender_timeout_seconds` from `build_targets.conf`.
When no hold remains, the enforcer removes the marker folder.
The message never changes a hold file or releases a hold.
`scan` reads state without changes and prints allowed, kept, or would stop, with the owner, reason, and pass time.
Status and the hold-start audit print kept runs and `--keep <pid>` for runs that would stop.
A read-only pass after release on 2026-10-28 took 0.585030 seconds over 1,736 processes.
This measurement sets no pass-time limit.
Other users' processes, such as the CI runner, are outside its reach.
Runs shorter than one pass can finish first, so call guards refuse those starts.

**Build call guard.** `settings.json` installs `scripts/hooks/pre-tool-use-build-hold-guard.sh` for Bash PreToolUse. 
The Bash front reads stdin once.
It starts no Python when the hold directory has no entry.
It also starts no Python when the input has none of `cargo`, `merge_gate`, or `targeted_test`.
 An unreadable hold path also starts the decision reader. Otherwise it passes the same input to `pre-tool-use-build-hold-guard.py` and relays its output and status. The guard refuses raw building `cargo` verbs (including short forms and `+toolchain`) while a non-holder natedev hold applies; removal ends that holder’s hold. All `verify.sh` calls pass, with or without a step record: verify routes eligible steps to the Mac during the hold and waits for other steps. Non-building calls pass. The Python guard checks the command through `tool_call_text.py`, checks for a hold entry, then imports `scripts/build_hold/hold_applies.py` in the same process. The helper uses `target_state(local_target())`, `natedev_holds` and `caller_holds_only_own()`. The sole in-force holder may build. Other sessions see `natedev builds are held by <holder>. Keep working, writes included. Use verify.sh: eligible steps go to the Mac; other steps wait. Raw cargo and merge gates must wait for the hold to end.` Only in-force holders are named, with `holder_text`. The helper exits 0 with that refusal on stdout, 1 silently when builds may run, or 2 with a read error on stderr. The guard denies hold read errors and any other exception in its decision path with a one-line reason that names the failed read. Only an explicit allow decision permits a build; no wait is registered.
The cargo verbs include `mend` and `miri`; the guard does not match `verify.sh` step names or options.
The guard also refuses `merge_gate.py` and `targeted_test.py` in every mode under the same hold decision.
This applies to direct calls and calls through Python or `lib/py`.
The refusal tells the caller to run after the hold ends.
Quoted mentions and written examples pass.
Codex workers use the same Bash front and guard through `scripts/hooks/codex_hooks.py`.
The staff run pointer identifies the director for workers of the sole holder.
Those workers may build.

**CLI.**

| Command | Behavior |
| --- | --- |
| `clear [target] [--holder N] --for TEXT [--keep PID]... --release-eta HH:MM --zone IANA` | Default natedev. A missing ETA returns exit 2 without changes. Use `HH:MM` for later today or `YYYY-MM-DDTHH:MM` for a later date. Starts the enforcer and prints its notice, keep list, and audit. Stops every test and build outside the allow rules. Registers each stopped session for release. Repeated holds add kept PID/start pairs and reset release fields. Other targets return exit 2 without changes. |
| `drain [target] [--holder N] --for TEXT [--keep PID]... --release-eta HH:MM --zone IANA` | Requires the same future ETA as clear. Keeps existing work and holds new work until explicit release. |
| `quiet [--max-wait 600]` | Waits for remaining builds; busy while any user's `cargo`, `rustc` or `cargo-nextest` run; counts each command by user (`your builds still running: 7 cargo, 5 rustc; ci-runner: 2 rustc`). Exit 1 when busy. Load does not delay it. Exit 0 prints `builds are quiet: no build runs. 1-minute load <load> is other work: <five heaviest processes>`, with user, command and current CPU percent over about one second, or `none visible`. Only success takes the CPU window: two reads of `/proc/<pid>/stat` user and system ticks divided by elapsed clock ticks; processes that end or use no CPU in the window are skipped. |
| `wait` | Returns at once through the shared local hold check. Exit 0 permits a build only when no other session’s holder file exists. The sole holder may build. Exit 10 refuses and registers the session for release notice; exit 12 means unreadable state. Cycle release fields never grant permission. Start only on 0; after 10, wait for release and check again. |
| `record-recipient --session-id --name` | Written by `top_level.py`. |
| `release [target] [--holder N]` / `release --resume` | Removes its holder file first, then records released_at and release_started_at; notices follow after the last file is gone. Other holder files keep the hold in force. One notice per recipient; the memory gate orders builds. `--resume` refuses while any holder file exists. Other targets return exit 2 without changes. |
| `status` | One line per holder with since, reason and ETA, its keep list, cycle delivery rows, and the audit with `--keep <pid>` for each run that would stop; or `no build hold`. Then `release pending:` lines name unfinished releases and their unsent recipients, using the release drain's reader, and `/build:hold release` to finish them, even with no holder file. A damaged record or cycle is named with its path, error and the command that sets it aside. Status changes no file. Present files stay held despite cycle release fields. |
| `audit` | Reads running build processes from `/proc`. Prints session-owned builds by class. Records other_session and no_session builds when a hold is in force. |

**Build audit.** `audit_builds(proc_root, holders)` reads `cargo-tile-real`, `cargo`, `cargo-nextest`, `rustc` and `rustdoc`. One top cargo or nextest process is one build. Nested cargo, nextest and compiler processes are its children, including through a shell. A compiler with no cargo ancestor is a separate build. The audit reads each process start time before and after its other files. It checks the process and its ancestors again before classification. A parent with a later start time cannot own an earlier child. An unreadable environment does not remove the build from the audit.

The owner comes from `CLAUDE_CODE_SESSION_ID`, `owning_session(environment)`, or `CODEX_THREAD_ID` on the process or an ancestor.
Otherwise, the nearest `claude` or `codex` ancestor supplies the owner.
An agent without a session id has a command, PID, and start time as its identity.
The `holder` class uses the file key rule from `caller_holds_only_own`.
That session's hold must be the only hold in force.
The `admitted` class requires membership in `builds.slice` or a matching reservation owner from the service's bounded `view` query.
The audit compares both the PID and start time for the process and its ancestors.
An unavailable service causes the same audit error as an unreadable admission folder did before the service.
An inherited verify call id does not prove admission.
The other classes are `other_session` and `no_session`.
These classes describe ownership and admission; the enforcer applies its allow rules separately.
The printed audit omits `no_session` builds.
The audit sends no messages and stops no processes.
The `clear`, `drain`, `status`, and `audit` commands accept `--proc-root` for a fake process tree in tests.

The audit calls `hold_enforcer.record_build(..., action="audit")` for each `other_session` or `no_session` build. The enforcer uses the same writer with `action="stopped"` for each stopped root. Both call `store.append_line` in the current process, after the audit read; no detached audit recorder runs. The kernel boot id, PID and process start time identify the build. The permanent file is `buildlog/<host>/hold-builds-<hold hash>.jsonl`. A separate `.lock` file covers the duplicate check and append, once per build, holder key, `since` and action. A new hold or a reused PID gets a new row. Index schema 21 adds `action` to `hold_builds`; old rows read as `audit`. Rows record hold identity, build identity, class, owner, command as shell text, working directory and observation time. Holder and admitted builds get no audit row.

**Holder key.** A staff holder uses its fixed staff ID as the file key. Without `--holder`, `CLAUDE_CODE_SESSION_ID` must resolve through `staff.snapshot().by_current_session`. A given holder resolves through its fixed ID, current session, or current staff name. An explicit holder outside staff keeps its given text as the key. Registry names and former names do not select holds or release records. `Holder.name` shows the current staff name when the key resolves. `cycle["holders"]` retains the key from `start_hold`. `holder_path` replaces unsafe file-name characters in that key. A recorded release matches the resulting file name and its `since` instant. The deployment converter handles old keys once; runtime readers accept only the fixed key.

**Hold cycle.** `release_directory()` uses `BUILD_HOLD_RELEASE_DIR` when set.
Otherwise, it uses a sibling of `holder_directory()` named `<hold folder name>-release`.
With neither variable set, the release folder remains `~/.local/state/build-hold-release`.
`current` names `<id>/cycle.json`.
The writer replaces this file atomically under `release.lock`.
`HoldCycle` stores its id, open time, and holders with `{since, released_at}`.
It also stores recipients by session id, release start, and `ReleaseEntry` rows with session id, name, and delivery outcome.
The first hold with no files opens a cycle without reading an old one.
With other holders, it joins the cycle.
Without a readable cycle, it writes its holder file and opens no cycle.
Readers still accept old cycle files with call, generation, and gate fields.
These fields no longer control a build.

**Release** (`release_cycle`). Under `release.lock`, `release_hold` first removes the holder file and queues the `ended` event. Only then does `release_cycle` record the holder's `released_at`. With another holder file present it prints `released; still held by <holder> (for ..., release eta ...)` and sends no notice. After the last file is gone it records `release_started_at` and sends notices. A stopped release leaves either a present file that still holds, or no file and a released hold; cycle fields grant nothing. All `wait-local` calls can then return and enter their memory gates. No waiter acknowledgement or turn can delay release. The memory gate orders the builds and starts each only when memory is available.

After the file is gone, `notify_release` checks each recorded recipient before it sends.
The notice says to start builds and BRP launches now; the memory gate orders them.
No answer is needed.
Claude session keys use `socket_for`, which returns `RecipientLive | RecipientGone | RecipientLookupFailed`.
Lookup exit 1 means the session is gone. Other lookup failures retry up to three times, one second apart.
Release reads one staff snapshot per cycle with notices to send.
Release sends Codex notices through `send.py --skip-closed --to codex:<thread> --text <notice>`.
The sender checks both machines for a route when it has no usable local route.
With `--skip-closed`, a complete search with no match returns `ABSENT` with exit 8.
An unknown Codex thread identity prevents the sender from confirming absence.
After automatic forwarding, the sender accepts a peer's `ABSENT` result only if its local search also proved absence.
When the roster shows only closed or stopped worker panes, send returns `CLOSED` with exit 7.
Release records `WorkerClosed`. The next assignment resumes the worker.
Release records a confirmed missing session as `RecipientGone`.
If the staff row is also absent, release removes the recipient from the cycle's recipient list.
Release recovery uses each session's first saved outcome to decide whether to restore a removed recipient.
Both routes use the same notice text.
Send exit 0 gives `Delivered`. Exit 1 gives `QueuedUnconfirmed`. Other failures give `DeliveryFailed`.
A failed Codex send prints the sender's error detail on stderr.
The cycle saves the outcome after the sender returns.
A stopped send stays pending and can repeat the notice on retry.
`Not reached, tell them yourself: <session name> (<reason>)` names failed and unconfirmed deliveries.
It also names a recipient whose staff row remains after its session is gone.
`cycle_status_lines` prints delivery outcomes.
Release waits for no answer, build or memory allocation.

`release --resume` supports a file left by an old release. It refuses while any hold is in force. With no cycle, the holder broadcasts the release. `open_cycle`, `save_cycle` and `write_hold` write a complete temporary file before `os.replace`; holder temporary files stay outside the holder directory.

**Release intent.** The last holder's release is saved atomically in `BUILD_HOLD_RELEASE_DIR/pending-release.json` before its holder file is removed. A new hold keeps an earlier pending record under `releases/<release-id>/`, so it cannot drop that release's notices. `release` and `release --resume` drain each pending record on its own when no hold is in force: a record whose notice or broadcast fails stays pending, and the drain goes on to the next. Under the release lock, a damaged pending record or referenced cycle is renamed with the same `.damaged-<UTC YYYYMMDDTHHMMSSZ>` suffix used for a damaged current cycle (`-2`, `-3` when taken). One line names its path, error and saved path, and the drain continues. A damaged or missing cycle is rebuilt from its pending intent's holder and recipients before sending; an uncertain delivery can repeat. A stop after the rename leaves the intent available for a rerun. A drained record becomes the one completed receipt for its holder key, at `receipts/<holder-key>/completed-release.json`, replaced in place, so receipts do not grow; older receipt folders are moved there on release lookup. A release without a usable cycle keeps its broadcast pending until the broadcast returns. Live holder files take precedence over receipts. A finished release prints `hold is already released.` even after another cycle opens, followed by the details of any record it drained, `Not reached, tell them yourself` lines included.

`status` reads current and retained pending releases without taking the release lock or changing files, including old receipts. Each `release pending:` line gives the holder, release time in the user's zone, unsent names and `/build:hold release` to finish it. Status and the drain call `release_notices` to validate the referenced cycle id and select the same first entry for each recorded recipient. A missing cycle entry, `NotAttempted` or `DeliveryFailed` means unsent; a broadcast says `every session (broadcast)`. An unreadable record or cycle, including an id mismatch, is named by path with its error and the command that sets it aside. Completed receipts add no pending line.

**Damaged record.** `read_cycle` validates the `HoldCycle` shape, session identities, and delivery outcomes.
Each holder's `since` must have a time offset.
It raises `ReleaseRecordReadError`, a `ValueError`, that names the fault.
An invalid holder time uses the same recovery path as another unreadable record.
Changing commands call `read_cycle_for_change() -> HoldCycle | NoCycle | DamagedRecordSetAside(path)` under the lock.
On damage, it renames `cycle.json`, including a dangling symlink, or `current` when no cycle file exists.
The new name is `<name>.damaged-<UTC YYYYMMDDTHHMMSSZ>`, with `-2`, `-3`, or later suffixes when needed.
It removes `current` and prints `release record could not be read (<error>); set aside as <path>; this hold now releases every session at once`.
Read-only `status` prints `release record could not be read: <error>; /build:hold release sets it aside and ends the hold`.
It changes nothing.

**Recorded hold intervals.** A new holder file queues a `began` event after writing the file; every removal in `release_hold` queues `ended`, using that file's key, name and reason. Repeating a hold writes no event. The event's UTC `at` is taken under the coordination lock and queued there; a detached `record.py hold-event` starts after the lock closes. A stalled event log cannot delay hold or release. Both reports read current coordination state independently. A lost event reads `end not recorded` or `began not recorded`, never a changed hold. The index keeps one row per event with its source file; the `holds` view pairs by host, target and key, ordering equal instants began first. Overlapping holders count once in held duration. A present holder file keeps an interval with no recorded end open, regardless of cycle release fields. Times shown use the user's zone, stored instants UTC.

**Target selection.** `clear`, `drain`, and `release` accept one optional positional target.
The shared target reader supplies configured names and `default_target`.
Only natedev acts in this delivery.
Mac, AWS, and `all` print one unsupported-command line and return 2 without changing state.
The parser rejects `--target` and the trailing `target <name>` form.
The retained remote helpers remain unreachable from these commands.
`/build:mac block` and `/build:mac unblock` retain the Mac workflow.

**No-cycle release.** With no cycle, `release` calls `release_hold` under the lock; the last holder's line becomes `released, builds may resume. No hold cycle: broadcast this release to every session.`, and the command doc has the holder broadcast it with `/notify_top_level --here`. A cycle release's final line never carries that clause.

### Broadcasts to sessions (`scripts/message/top_level.py`, `commands/notify_top_level.md`)

`top_level.py` takes no arguments (any argument: `usage: top_level.py`, exit 2) and runs no ssh. `forwarded_sessions()` reads this machine's roster (`roster.roster(os.environ, roster.ThisMachine())`, `docs/as-built/session-roster.md`) and keeps each `roster.is_top_level` row: a Claude session that is neither a worker nor a unit director under a showrunner, so a unit director with no running showrunner is included. It leaves out the sender by `roster.sender_identity` and `roster.is_sender`: `CODEX_THREAD_ID` first, then `CLAUDE_CODE_SESSION_ID`; with neither set nobody is left out. Roster problems print on stderr as `top_level: roster: <problem>` and the rest still prints. Each row's socket comes from the newest running record for its session id (`sessions.running_sessions()`), so a conversation two processes hold (resumed in a terminal while the desktop app still has it open; seen 2026-10-08 as `bevy_brp` listed twice) is listed once, and two sessions that share only a name are both listed. Each row becomes `AddressableSession(name, session_id, address) | UnaddressableSession(name, session_id)`. Only addressable sessions print, one `<name>\tuds:<messagingSocketPath>` line each, sorted by name then address, and are recorded as hold recipients (`build_hold.py record-recipient`; `main` returns 1 when a record fails); each unaddressable one prints `not reachable: <name> [<session id>] has no messaging socket` on stderr. `/notify_top_level` sends to each `uds:` address, adds every ListAgents peer on another machine unless `--here`, asks showrunners to pass the message to their unit directors, names held or refused deliveries by name and address, and reports each `not reachable` line as a session not told.

2026-11-11: Dailies and their build-hold footer were retired.
Use `build_hold.py status` to read holder and release-delivery state.

### Disk floor: what it removes (`scripts/lint/sweep.py`)

While `/` has less free than `sweep_free_floor_gib.<host>` in `config/lint.conf` (300 GiB on natedev), `hold_floor` removes build output from the cargo target directories under `sweep_floor_roots` that no build holds. It removes only the shortfall (`budget = total - (floor - scanned_free)`, with `scanned_free` read after the scan that measures the caches, so space another process frees during the scan is not taken again; at or above the floor after the scan it takes nothing), from idle targets in last-use order, oldest first, across all locations (`/tmp` targets have no special priority; `build-targets.md` → Build output retained by the sweep):

- A target that goes away during a pass counts as done. The pass finishes with status 0. Its bytes do not count as removed.
- **What a target is.** `is_cargo_target(directory)`: a directory holding `.rustc_info.json`, or cargo's `CACHEDIR.TAG` (matched on its line `# This file is a cache directory tag created by cargo.`) beside a build tree (`build_trees`). `target_dirs()` and `disk.py`'s `outside_directory_bytes()` both call it. Either marker alone misses targets: into a folder cargo creates, the tag and `debug/` appear at once and `.rustc_info.json` comes when the first build ends, or never; into a folder that already exists (mend's wrapper makes hana worktrees' first) cargo writes `.rustc_info.json` early and no tag.
- **Targets in use.** A target is busy while a build holds its Cargo locks.
  It is also busy when a live process uses an executable, working directory, or mapped file under it.
  `process_uses()` checks `/proc/<pid>/exe`, `cwd` and `maps`.
  A cargo-nextest or `verify.sh` process also keeps its workspace targets busy, including time between test binaries and time in the build queue.
  The scan recognizes `verify.sh` directly or as the first script argument after options to `bash`, `sh`, `zsh` or `dash`.
  `workspace_targets()` resolves `--target-dir`, `CARGO_TARGET_DIR`, Cargo config, or a target beside an ancestor of the working directory.
  The use text is `runs verify.sh in <cwd>`.
  nextest holds no Cargo lock during tests and executes binaries from `deps/`.
  A floor sweep at test 2105 of 4072 left 1965 hana tests unable to execute their binaries.
  `hold_floor` prints `lint sweep: pid <pid> <how>; <target> left alone` and counts the target as held.
  `sweep_workspace` prints `… skipped` and removes nothing from the busy target.
  A running app also keeps its target busy.
  Executable or mapped paths that end with `(deleted)` do not count as use.
  The sweep cannot remove or damage those files.
  A process with only deleted files under a target leaves it idle when its working directory is outside.
  A hung cargo-berth process held 26 GiB of cargo-liner-berth output for three days before this rule changed on 2026-10-21.
  One scan of `/proc` covers every target and runs only below the floor.
  A measured scan took 0.21 seconds over 110 targets and about 600 readable processes.
  Without `/proc`, as on the Mac, the scan finds no use.
- **Roots.** `sweep_floor_roots` is `~/rust`, `~/.local/state`, `~/.cache` and `/tmp`. `~/.cache` came in on 2026-10-21, after 421 GiB of idle targets under `~/.cache/trace-targets` had stayed unseen; it adds 0.4 s to the walk (21,000 folders). `~/.cache/sccache` holds no `.rustc_info.json` and no cargo tag, so it is never a target.
- **Agent scratch under /tmp.** `sweep.py --floor-only` calls `tmp_sweep.run_if_due` before `hold_floor`, so idle scratch goes before any build cache. It runs the sweep when `tmp-sweep.stamp` in the lint-sweep state folder is an hour old or more (`RUN_INTERVAL_SECONDS`). The stamp moves before the run, so a failed run also waits an hour. `tmp_sweep.py [--dry-run]` runs it at once. For hand cleanup, run `tmp_sweep.py --plan <new-plan.json>` to list every candidate with `keep` or `remove` and one reason, and save the plan without deleting anything. Then run `tmp_sweep.py --apply <plan.json>`; it judges every candidate again once, at its start, with the timed sweep's ownership and process-use checks, keeps planned paths that gained a keep reason, and logs each removal. It never deletes an unplanned path. Hand cleanup also requires end evidence: all recorded workers ended, an expired scratch task, or a recorded verify run that stopped. Age alone does not permit hand cleanup. On the Mac, the hourly `tmp-sweep` job runs `tmp_sweep.py`.
`modules/darwin/tmp-sweep.nix` in nixos defines this job.
The separate disk-floor job runs every 120 seconds.
 Each root is a real path, because the Mac's /tmp is a link to /private/tmp and lsof prints real paths. They are `/tmp/claude`, `/tmp/claude/delegate`, `/tmp/claude/unit`, each `-`-prefixed project folder in every `/tmp/claude-<uid>` this user owns (so Claude Code's own `bash-edit-diff/` stays), the scratch roots `build_folders.conf` `roots.scratch` and the former `/tmp/claude/scratch` (one entry per direct child; a change review row's folder stays until its verdict), and the temp folders: /tmp, `$TMPDIR` and the Mac's `CS_DARWIN_USER_TEMP_DIR`. In a temp folder only a tempfile folder and a `lint/invoke.sh` log file (`INVOKE_TEMP_NAME`: `lint_invoke.*.log`, `buildlog.*.log`, `.peak` and `.peak.cgroup`; a killed run leaves them, and natedev /tmp held 1,308 and 1,438 on 2026-10-27) are judged. A tempfile folder (`TEMPFILE_NAME`) is `.tmp` and 6 letters or digits (Rust's tempfile crate), `tmp.` and 8 to 10 letters or digits (mktemp), or `tmp` and 8 lower-case letters, digits or `_` (Python's tempfile), the names test runs leave behind (natedev /tmp held 2,662 Rust ones on 2026-10-26 and the Mac's temp folder 273; the first folder audit that day found 3,917 mktemp and Python ones). In /tmp alone (the wide root; `$TMPDIR` and the Mac's temp folder hold app state, and macOS clears its own) every other empty folder this user owns is judged too, whatever its name, and uses `lint.conf` `folder_remove_after_seconds` with the same process-use check. A folder that holds only folders, however deep, counts as empty (`holds_no_file`; a tree of more than 1,000 folders does not), and its idle time is the newest mtime in the tree (2026-10-27: 155 nix-shell folders of empty `hana-runtime` folders stayed). A `direct/verify.sh` run folder (`VERIFY_TEMP_NAME`: `verify-hold.<pid>.XXXXXX`, with the run's `verify-copy.<pid>.XXXXXX` folders inside it) is judged in every temp folder. Its EXIT trap removes it, but a killed run leaves it (natedev /tmp held 47 hold and 6 copy folders on 2026-10-27). It stays while the named pid runs `verify.sh` (a `verify.sh` argument in `/proc/<pid>/cmdline`, or `ps` on the Mac) and that process started no later than the folder's mtime, so a reused pid does not keep it; otherwise it goes at the next run. An old name with no pid takes the 4-hour rule. A folder there that holds something can hold work, so the folder audit judges it, with a git check first (2026-10-27; until then the sweep took any idle folder, and on 2026-10-26 1,103 month-old ones went by hand, an rm the user had to approve). A folder a system tool owns stays (`SYSTEM_TEMP_NAMES`: `claude`, `.X11-unix` and the other X folders; `SYSTEM_TEMP_PREFIXES`: `systemd-private-`, `tmux-`, `nix-`, `ssh-`, `claude-`, `com.apple.`, `powerlog`), except an empty `nix-` one (`EMPTY_PREFIXES`: nix-shell and nix develop leave theirs, 1,321 in natedev /tmp on 2026-10-27), as do another root and a folder that holds one. The sweep does not judge or log other entries in a temp folder. An entry is one top-level file, folder or link under a root. It goes when nothing in it changed in 4 hours (`IDLE_SECONDS`) and no process has its cwd, an open file or a mapped file in it (one `/proc` scan per run; where there is no `/proc`, one `lsof -n -P -w -F pfn` scan: `cwd`, open files, and `txt`/`mem` mapped files). A change is the newer of mtime and ctime, over everything inside the entry, except a folder's ctime in a temp folder or a session or run temp root (systemd-tmpfiles-clean moves it daily). The entry's own times count only for a file, a link or an empty folder. The roots stay. A staff row's `OpenRun` pointer keeps its run folder under `unit/` or `delegate/`. A staff worker also keeps the run when its pane state does not prove that it ended. A worktree in `progress_history_state.json` keeps the run when it belongs to a live or paused session. The shutdown mover records paused sessions. These runs and their temp entries stay when the director process stops. A running director recorded in `director` or the opening `run_started` history event also keeps the run. A kept run also keeps any folder that contains a `detached_runs/*.json` record's `cwd`, or is that working folder, only while that record has status `running`. Finished records do not keep worker folders. Only these records name worker folders; pending files are not read. An empty detached run file is not a record. Each current staff worker whose pane state does not prove it ended keeps its `/tmp/claude/session/<session_id>` parent; old entries inside that parent still take the 4-hour rule. An unreadable non-empty detached run record keeps scratch entries and top-level `/tmp/claude` entries and prints its path. It does not keep old temp entries in session roots. Before deleting an idle entry under any root, both timed and hand cleanup check for git worktrees down to three folder levels, without following links. Uncommitted changes, untracked files, a stash or commits absent from remote and local clone-source refs keep the entry. Git checks use `GIT_OPTIONAL_LOCKS=0`, a 10-second timeout per command, and never fetch. A failed check keeps the entry. The folder audit uses the same git check. The new reasons `a live worker uses it` and `it holds unsaved git work` include kept paths in the log. Unreadable staff, pane evidence, session, shutdown, or run ownership records keep the affected runs; a session folder named for a running session, though an entry of 1 GiB or more in its `scratchpad/` and `tasks/` is judged like any other (`SESSION_ENTRY_MIN_BYTES`; showrunner, 2026-10-25: a live session keeps small files there that it only reads, and a read does not move mtime); each of these when a session record cannot be read; an entry that cannot be read; and everything when neither `/proc` nor lsof can show which processes use the entries. Links are never followed, a read-only folder is made writable and deleted, and a project folder left empty goes. Each run appends one JSON line to `~/.local/state/lint-sweep/tmp-sweep.jsonl`: `deleted` (path, GiB, last change), `freed_gib`, `failed`, `kept` (a count per reason), and `kept_paths` (every reason except a recent change and a small entry in a running session's folder; an in-use path names its pid and how it uses the entry). Lines older than 7 days drop off. The sweep is the only remover under its roots: `disk-floor.nix` excludes `/tmp/claude` and `/tmp/claude-*` from systemd-tmpfiles' age cleanup (`x` rules). Its `q /tmp 10d` rule deletes each file in /tmp older than 10 days, and then each empty folder. On 2026-10-26 that rule had removed an idle unit's run folder and its run marker, one file at a time. The run also prints one line per deletion and a summary to the disk-floor journal. History: it replaced `remove_session_scratch` (2026-10-21: ended sessions only, 24 h idle) on 2026-10-25. That day /tmp/claude held 122 GiB and /tmp/claude-1000 held 86 GiB, and a one-time cleanup had deleted both `active/` folders. The first dry run, with the 1 GiB line, would delete 1,891 entries (20.4 GiB) in about 4 s. The rest stayed because processes used the entries (a 70 GiB target) or because running sessions and unit runs held them.
- **Use stamp.** `sweep_workspace()` sets the mtime of `.lint-sweep-used` (`USE_STAMP`) in every root `cargo_roots()` returns, before `lock_trees`, so a step that compiled nothing, or whose sweep was skipped because a build held a lock, still counts as use. A dry run and `--target-dir` (CI) write none; a write failure is ignored.
- **Target last use.** `target_last_use(root, groups)` is the newest of the stamp's mtime and the target's build units' `last_used`.
- **Order.** `shrink()` and `choose()` take a `RemovalOrder`, a sort key over `Group`; the smallest key goes first. The workspace budget sweep passes `unit_age` (most whole days unused, then oldest compile). For the floor's target order, see [Build output retained by the sweep](build-targets.md#build-output-retained-by-the-sweep). A scratch target a build holds is left like any held target. Orphaned files (`Scan.orphans`, one group per build tree) go first from any idle target, whatever the order.
- **Whole takes and nested targets.** An idle target with no build units is taken whole in the same last-use order (its folder and lock files stay), and a Cargo target nested outside another target's build trees is a target of its own: see [Build output retained by the sweep](build-targets.md#build-output-retained-by-the-sweep).
- **Lines.** After the removal lines, one line per target that lost output, in take order (oldest use first): `lint sweep: the floor took <gib> from <target>, last used <when>` (`would take` on a dry run). Orphan bytes count toward their target, and a group whose removal failed is left out, so each line is GiB actually taken. The removal line's `last used <oldest> to <newest>` spans the oldest and newest unit among those chosen.

On natedev (2026-10-08) the target rule finds 76 targets where `.rustc_info.json` alone found 41: all 35 added are scratch folders under `/tmp`, 31 of them finished builds carrying only the tag, and five under `/tmp/ct-g1l`, outside both Claude temp folders. The walk takes 2.2 s (1.5 s before).

Measured over the 24 h after the target-first order went live (2026-10-06 16:33 PDT, 300 GiB floor): the 2-minute timer took 84.0 GiB in 17 sweeps, 98.3% of it from targets idle over 6 h, and the newest unit a sweep took was last used a median 15.2 h earlier. The 24 h before, under the old order by build unit and almost all at a 500 GiB floor: 870.1 GiB in 155 sweeps, median 3.3 h. The two days differ in floor as well as order, and the memory gate above went live 85 minutes into the second.

### Scratch folders (`scripts/lib/scratch.py`, `scripts/hooks/pre-tool-use-scratch-guard.py`)

`scratch.conf` holds the default lifetime, hold file, repository depth, excluded folders, and random suffix length.
The scratch code reads those values through `scripts/lib/conf.py` when an operation needs them.
`CONF_DIR` selects the configuration folder.

The folder audit reads clone age from `hana_instances.conf` and the Mac disk floor from `mac_test.conf`.
It reads its other limits and default state folder from `lint.conf` through the same reader.
The audit and temporary sweep share `folder_remove_after_seconds`.
The audit and repository scan share `scratch.conf` `repo_skip`.
`rust_roots.txt` remains a list file. All moved values retain their previous defaults.

Agents made loose repo copies, repros and test trees in /tmp and ~/rust, and nothing removed them.
Sessions run for weeks, so a session end is no cleanup point (user, 2026-10-26/27).
Each scratch folder belongs to a task. The task's end removes its folders.
`scratch.py new [--task <task> | --until <when>] [--name <word>]` makes a folder and prints its path.
`scratch.py new --worker [--name <word>]` makes a folder under `TMPDIR` for the caller's worker task.
The DONE completion hook ends that task before pane closure. Other workers' folders remain.
`scratch.py list` shows each folder's task and last change.
The other commands are `adopt`, `hold`, `release`, `renew` and `end`.

Tasks use these forms:

- `run:<run id>` ends through `end_session.sh`.
- `phase:<unit>/<phase>` ends through `merge_checkpoint.py` after the phase's code checkpoint.
- `gate:<id>` ends through the gate's caller.
- `row:<showrunner>-C<n>` ends through `change_review.py verdict` after either verdict.
- `until:<YYYY-MM-DDTHHMMZ>/<id>` ends through the hourly temporary sweep when its time passes.

`--until YYYY-MM-DD [HH:MM]` selects a deadline in the user's time zone.
Each call creates a random task id from `scratch.conf` `suffix_length` characters.
Folders with the same deadline have separate tasks.
The folder name includes the deadline stamp but excludes the task id.
With no task, each folder gets a separate deadline task that lasts 24 hours.
The deadline reader also accepts old tasks without an id.
`scratch.py end --task <task>` selects folders by their exact task.

Creation and transfer accept a `phase:<unit>/<phase>` task only when the phase id appears in that unit's recorded plan.
A missing or unreadable plan refuses the new task and names the cause.
Use a recorded phase id, `gate:<id>` or `--until <when>`.
Existing tasks with an invalid phase id can still update and end.
The sweep keeps a task's folder while the task remains open.
It removes folders after seven idle days, even when the task remains open.
A plain file directly in the scratch root reads as `NotRegistered`.
The sweep uses the existing idle limit for unregistered entries.
An unreadable folder record still keeps the folder.

`scratch.py hold <path> --reason <why>` keeps a folder at its path until its task ends.
`change_review.py add --hold <path>` also creates a hold.
The hold record stays in `~/.local/state/scratch/holds.json`.
`end` removes the held folder unless a repository in it has uncommitted changes, a stash or commits absent from all remotes.
The folder stays with its hold marked ended.

A folder made without `--worker` uses `<scratch root>/<kind>-<name>-<word>-<6 chars>`.
The scratch root is `build_folders.conf` `roots.scratch`: `/tmp/claude/scratch` on natedev and on the Mac.
An `until:` task's folder is `<word>-<6 chars>`, or `scratch-<6 chars>` with no word. Its record keeps the time.
Each folder holds `.scratch.json` with its task, maker staff id, and times.
A Git worktree uses `git worktree remove` for removal.
A failed removal prints a warning and does not fail the merge or the verdict.

The scratch guard, a `PreToolUse` hook on `Bash` for Claude Code (`settings.json`) and Codex (`codex_hooks.py`), refuses a command that makes a new folder directly in /tmp, `$TMPDIR`, /tmp/claude, ~/rust or ~/rust/scratch (and judges a Write or NotebookEdit file path where its matcher names them): mkdir, mktemp -d, git clone, git worktree add, cp -r/-R/-a, rsync, cargo new/init, and tar extraction into a root. Allowed there: the session scratchpads (`/tmp/claude-<uid>`), `/tmp/claude/unit`, `/tmp/claude/delegate`, `/tmp/claude/session` and both scratch roots; ~/worktrees is no root, and a `make_a_worktree` command is never judged. A heredoc body, a quoted argument and a comment are data. A path the hook cannot follow (a variable other than HOME or TMPDIR, a command substitution, a command inside `bash -c` or ssh) is allowed, and the hourly tmp sweep is the backstop. A `TMPDIR` inside a session scratchpad, a session temp root or a run folder is no root.
Before its checks, the scratch guard uses the session-start path helper to restore only the input session id's missing temp folder with mode `0700`; an existing folder costs one `os.stat`, a missing or invalid id creates nothing, and a repair error allows the tool call to proceed to the checks.

Each Claude session's `TMPDIR` is `/tmp/claude/session/<session id>`: `hooks/session-start-temp-root.py` makes it at start, resume, clear and compact, and writes `export TMPDIR=...` to `$CLAUDE_ENV_FILE`. A run's workers and `verify.sh --session-dir` use `<run folder>/tmp`. So mktemp, tempfile and Rust `.tmpXXXXXX` folders land there, and the hourly tmp sweep judges each entry with its 4 h rule; the root goes only once its session or director stops, and nothing waits on that. In /tmp and the other temp folders a folder's ctime is no change, since `systemd-tmpfiles-clean` moves it every day (its 26 Oct run kept month-old folders such as `cargo-installQOkrzS` looking fresh to the 7-day rule).

The daily folder audit (`scripts/lint/folder_audit.py --send`) checks loose folders that can hold work.
A `nate.jobs` job runs it on natedev and the Mac.
It checks ~/rust, ~/rust/scratch, /tmp, /tmp/claude and `$TMPDIR`.
In ~/rust, `config/rust_roots.txt` and `scratch` name kept folders.
Every folder in ~/rust/scratch is loose.
In temporary roots, system folders, session scratchpads, scratch roots and run roots stay.
The audit also keeps `/tmp/codex-daemon-<uid>`, which holds sockets for running Codex processes.
The sweep checks tempfile folders, `verify.sh` folders and folders that contain only folders.
If the audit cannot read `config/rust_roots.txt`, it does not check ~/rust.
Only folders owned by this user and idle for a day enter the audit.
The audit checks modification times down to three folder levels and each repository's `HEAD`, `index` and `logs/HEAD`.
It skips `target/`, `node_modules/` and `.git/` during that scan.
It keeps folders with more than 50,000 entries at that depth.
A loose folder idle for `folder_remove_after_seconds` can be removed after seven days.
An open hold, process use, live socket or unsaved git work keeps it.
On the Mac, any socket in a folder keeps it.
A failed git check also keeps it.
The audit prunes removed linked worktrees from their source repositories.
It records each removal in `~/.local/state/folder-sweep/removed.jsonl`.
`folder_audit.py arm` enables removal, and `disarm` disables it.
Before arming, each selected folder prints `would remove`.
The first dry runs on 2026-10-27 found 106 loose folders on natedev and 13 on the Mac.
They selected 36 and eight folders for removal.

Phase 44 moved the Mac runner from the standing `~/rust/hana_catalyst_mac` clone to scratch clones.
The old standing clone had no lifetime after that change.
The director removed its 85.8 GiB target on 2026-11-08 with builds' approval.
That removal closes the Phase 44 gap for the old target.
Each folder creator must register its lifetime.
`~/.cache` remains outside the Mac cleanup roots.

The Mac audit log last changed on 2026-11-07 at 14:13:40 EST.
Its saved runs predate the deployed `/usr/sbin` PATH repair.
The old launchd PATH could not resolve the system `lsof`.
The audit therefore kept due folders because process use was unavailable.
The loaded `org.nixos.folder-audit` job now includes `/usr/sbin` in its PATH.
Its plist has a change time of 2026-11-08 at 11:04:48 EST.
The inspection does not expose the exact job load time.
The 2026-11-08 inspection reports zero runs and no previous exit for the loaded job.
The job has `RunAtLoad=false` and an 86400-second interval.
No audit result after the PATH repair exists in that inspection.
The next scheduled result must establish the removed count and the count of unavailable process-use rows.

The maker is the earliest-started session whose own command creates the folder.
The audit uses the scratch guard's `commands()` and `targets()` parser and resolves targets against the transcript row's `cwd`.
Read-only commands, command output and message text do not prove ownership.
One ripgrep pass selects possible transcript rows by folder name before the parser checks their commands.
Each proven maker receives one message with its folders, outcomes, `scratch.py adopt` and `scratch.py hold`.
A folder without a proven maker prints `maker unknown` and receives no message.


### Disk-floor alerts (`scripts/lint/sweep.py`, `scripts/buildlog/disk.py`)

Each machine keeps its crossing state in `${LINT_SWEEP_STATE_DIR:-~/.local/state/lint-sweep}/floor.json`.
After a non-dry pass, natedev and the Mac use the same phone-floor crossing path.
A reading below `sweep_phone_floor_gib` opens an episode and sends a message to the natedev session.
The message names the Mac when its disk is low.
A current `sweep.disk_floor` text approval also permits one phone push per episode.
The push names the machine and recommends one measured folder, or asks for measurements when folder sizes are unavailable.
Each channel keeps its own receipt; a failed channel can retry without repeating the accepted delivery.
Every pass must read above the alert line plus `sweep_phone_rearm_gib` for `sweep_phone_rearm_minutes` before the crossing rearms.
The crossing record keeps the first above-line time under the record lock.
A reading at or below that boundary restarts the interval.
`config/lint.conf` sets the margin to 5 GiB and the interval to 60 minutes.
With the 150 GiB alert line, free space must stay above 155 GiB for 60 minutes.
Rearm sends one `sweep.disk_floor_recovered` phone push after an accepted fall push.
The return receipt and its stable key prevent accepted return delivery from repeating.
A failed return delivery retries above the rearm line.
A later fall starts a new episode even when the previous return receipt stays pending.
At recovery, `disk_floor` retires every open crossing's `phone:<identity>` fall key on natedev, which owns the silence queue.
This includes a crossing whose fall phone receipt stays pending.
The Mac reaches this retirement through ssh natedev.
If retirement drops a kept fall push, the fall never reached the user and no return push sends.
The reported dropped count determines this result even when the retire command exits with an error.
A retire attempt without a dropped count suppresses the return push in the saved crossing state.
The next pass retries retirement; a completed retry closes that crossing without a return push.
Only an accepted fall and a first retire result of zero dropped pushes permit a return push.
An episode with a pending fall phone receipt gets no return push.
The same rule applies to natedev and the Mac.
While text approval is stale, the natedev message still sends and the phone receipt stays pending.
The phone channel can send that pending episode after the showrunner records the new approval.
The return text needs its own approval before delivery.

Natedev also records cache sizes, growth and removal accounting. These reports stay outside the Mac path:

- **Floor out of reach.** The sweep reports this state when it selects every removable unit and free space remains below the floor. It uses the `push_history` interval. It never asks the user to free space by hand (2026-10-21). `floor_holders` identifies each busy target, its size, its holding process and its session. `process_uses` finds the process, or `lock_holder` reads `/proc/locks` up to `sweep_proc_locks_reads` times for a held Cargo lock. `holder_sessions` finds the nearest ancestor with a live session record through `process_table.nearest_named`. Each session holding busy targets gets `holder_message_text` through `send_holder_message`. natedev gets `floor_out_of_reach_text`, which lists busy targets and the sessions that received a message. This report sends no phone page.
- **Growth** (free space fell more than `sweep_unexplained_fall_bytes` 10 GiB beyond build-cache growth since a floor sweep at most `sweep_growth_window_seconds` 30 min earlier; this sweep's free is read after the scan that measures its caches (`scanned_free`), and the record's free before its caches walk, so a target another process deletes during either walk cannot read as a fall. On 2026-10-25 a free read from before the scan reported a 122.6 GiB fall while 123 GiB was freed. A target held by a build hold counts 0 bytes, so the growth check runs only when the previous record's `held_roots` equals this pass's held set; an older record without it skips the check. The sweeper records its own last delete per target in `target-deletes.json` beside `floor.json`, and a directory mtime no later than that time plus 2 s does not make a target held. On 2026-10-29 its own deletes made a 34.7 GiB target look written during a hold, and a false 36.4 GiB fall was reported) and **large removal** (more than `sweep_large_removal_bytes` 32 GiB): natedev alone (`send.py --to natedev --from disk_floor`), at most one per `sweep_alert_interval_seconds` (1 h) counted from the last delivered alert.

`send_floor_alert()` delivers the session message; exit 0 or 1 counts as accepted.
`send_phone_push()` delivers the separate phone push; only exit 0 counts as accepted.
Folder choices come from `disk.json` as `AvailableDiskMeasurement | UnavailableDiskMeasurement`.
The choices use the same held-output rule as the Mac choices described below.
`FloorOutcome.held_targets` carries live-use paths, including protected targets with live use.
The floor pass sets these paths when it sends the notification.
It includes live use found during the deletion checks after the scan and includes paired output roots.
`HeldOnlyFolderChoices` identifies a measurement that contains no folder without held output.
`buildlog disk` records large directories outside Cargo targets, their growth and their largest child.
It also records `outside_build_cache_totals` and `previous_measured_at`.
`send.py`'s `log.jsonl` lines carry `text`.

### Rust release trial

`rust_release.py` keeps a trial ended by a kill (status 128 or more, -9/-15, or signal 9/15 or SIGKILL/SIGTERM in its output) at `waiting` with reason `<step> killed; retry next night`; a genuine failure stays `failed`.

### `verify.sh` example guard

`verify.sh build [<pkg>] [--bin <name> | --example <name>] [--release] [--features <list>]` builds the selected binaries or example.
`BUILD_TARGETS_PY` reads the package's binaries, or every member's binaries when no package is given.
A named example supplies its required features.
`take_build_args` refuses a missing target or a package without a binary with exit 2.
The Cargo build keeps `--workspace` and the workspace's feature set.
A named package owns feature names without a slash; without a package, feature names pass unchanged.
The build uses the build hold, cargo token, memory admission, and a call record with verb `build`.
`print_built_binaries` prints `binary: <target_directory>/<debug|release>/[examples/]<name>` for each target.
The build saves no pass record and does not use Mac offload.
Use this command for smoke and shot helpers.

A gate run is `test [<pkg>]` without `--filter` or `--test`; `--features` is allowed.
A test without a package uses the workspace figure for memory admission and estimates.
Gate runs and `final` read `cargo metadata --no-deps --format-version 1` once into `GATE_METADATA`.
They run `UNTESTED_EXAMPLES_PY` before the cache lookup and any build.
An example is invalid when a source line matches `^\s*#\[(cfg\(test\)|(\w+::)*test)\]` or its manifest sets `test = true`.
The scan reads every `.rs` file inside a directory example (`examples/<dir>/main.rs`).
Each source violation prints a reason and a correction:

- `example <name> holds a test: <file>:<line> has <attribute>, and examples carry no tests.`
- `move it into <package>'s src/ or tests/, or delete it.`

Each manifest violation prints:

- `example <name> sets test = true in <manifest>, and examples carry no tests.`
- `remove test = true from its [[example]] entry.`

After all violations, the call exits 2 with no Cargo call except `metadata`.
`read_metadata` reuses `GATE_METADATA`.
`verify.sh example-test` exits 2 before any work.
It prints `verify.sh: example-test is removed: examples carry no tests (user, 2026-10-04).`.

## Invariants

- Every compiling step and every BRP launch passes the memory gate before it starts. Sweep, `cargo fmt` and `taplo` steps never wait on it, so the disk floor is never held by memory.
- Admission and hold check errors stop the step. Only an explicit admission answer allows it to start; a wait limit grants nothing.
- The disk floor never removes output from a target whose cargo locks a build holds, removes no more than the shortfall plus the last group's overshoot, and prints no new line containing `removed` (`scripts/buildlog/parse.py` counts those lines as freed bytes).
- Build steps skip the Python hold check when the holder directory is missing or the shell confirms a readable, searchable, empty directory. A memory wait never starts a build without memory.
- `BUILDLOG_MEM_WAIT_S` and the gate outcome are set in the calling shell before any `| tee` pipe; a step's recorded start excludes the wait.
- The sccache server stays in `builds.slice`; moving only the steps would leave every compile outside the limit.
- Raising a step's oom_score_adj never stops or alters it: a failed write is silent, the `|| true` keeps the scope shell running when a caller exports `SHELLOPTS=errexit`, and exit status and output pass through.
- A memory kill is never recorded or cached as the tree's lint or test failure; it re-runs once and stops at the second.
- A cargo token is reclaimed early only through a `holder_pid` that names a gone process; holder and waiter share one pid namespace. The guard file is never removed.
- Holder files supply current hold state. Old cycles distinguish files still in force from completed holds. A release removes its holder file at once. Historical log rows never decide whether a build can start.
- Releases go by session id, never by name. A session that cannot be messaged is never recorded as a recipient.
- Hold changes use `release.lock`. Release delivery uses the ended cycle after the holder file is gone. A damaged record is set aside by a changing command, never by `status`.
- `steps.mem_wait_s` is the memory wait. `steps.wait_s` is the call's lookup and token wait. The file name hashes the schema SQL. Bump `SCHEMA_VERSION` only when the way rows are derived changes.
- Only `ran` CI jobs carry duration and queue time. CI days key on the run attempt's `started_at` everywhere.
- The window report is host-scoped; the daily report aggregates hosts. A run's own peak comes from `ci_anon_bytes` / `builds_anon_bytes` samples, never from `memory.peak`.
- Only a phone-floor crossing reaches the phone, with current text approval; diagnostics go to natedev.
- Tests never read the real cgroup tree, `/proc/meminfo`, `/proc/pressure/memory`, journal or transcripts, and never write `~/.local/state/buildlog`, `build-hold`, `build-hold-release`, `notify` or the agent notes: they take `BUILDLOG_MEMINFO` and `BUILDLOG_MEMORY_PRESSURE` from `scripts/lint/memory_test_support.py`, set `BUILDLOG_*_CGROUP`, `BUILDLOG_TRANSCRIPTS`, `BUILD_HOLD_DIR`, `BUILD_HOLD_RELEASE_DIR`, `HOME`, and stub `journalctl`, `zramctl`, cargo and git on `PATH`. Tests set the hold variables for a checkout's scripts; installed scripts refuse to run with them set.
- When `BUILDLOG_LIVE_MEMORY_TRIPWIRE` names a record file, the shell gate and Python checker refuse a path that resolves to `/proc/meminfo` or `/proc/pressure/memory` before reading it, append a JSON record, and name the fake readings the test must set. The merge gate sets this variable for every test module and fails any module with a recorded live reading. The default-path test removes the tripwire but supplies fake pressure text. A failed record write still refuses the reading but leaves no record, so the gate detects only recorded readings.
- A verify.sh routing test follows `scripts/direct/test_verify_untested_examples.py`: cargo passes `metadata` to real cargo and logs every other call, git is stubbed, and the environment drops `UNIT_RUN_DIR`/`UNIT_TEAM_ROLE` and sets `BUILDLOG_OFF=1`, `BUILDLOG_SCOPE=0`, `BUILDLOG_MEMINFO`, `BUILD_HOLD_DIR` and `CARGO_TARGET_DIR`. Tests set `BUILD_HOLD_DIR` for a checkout's scripts; installed scripts refuse to run with it set.
- Times shown are in the user's zone through `user_zone()`; stored instants are UTC. An `HH:MM` release ETA names the zone in which it was supplied.

## Calibration and gotchas

- **Unfaked memory pressure.** On 2026-10-29, `test_brp_launch_gate.py` took 169 s against a 6 s median because its pressure reading was unfaked.

- **Memory gate sizes.** The first need comes from `program_first_peak_gib.cargo` in `config/build_admission.conf`. Build steps use shape tiers. Other steps retain measured, build-log, and pooled tiers. The service reads process memory before host memory during each pass.
- **One admitter after a hold release.** The service reads memory, decides, and persists grants in its loop. Each step retains its accepted need unless an observed test phase selects its frozen test-phase need. Swap-backed use reduces its keep-aside. Simultaneous callers cannot reserve the same room.
- **Release outcomes vanish with the last holder file**; set-aside `*.damaged-*` files are never cleaned up. A holder joining a no-cycle hold learns nothing until the final release line tells it to broadcast.
- **earlyoom never logs "killed";** its line is `sending SIGTERM to process <pid> uid ... "<comm>": ...`.
- **zram is invisible to slice limits.** earlyoom counts zram's virtual size as swap; `MemoryMax` counts RAM only, and compressed pages are charged to no slice.
- **`MemoryHigh` on a slice holding a heartbeat** (the runner's `Runner.Listener`) stalls it until GitHub drops the runner; such a slice gets `MemoryMax` only and its services `OOMPolicy=continue`. `MemoryHigh` on `builds.slice` throttled builds not short of memory, since it counts page cache.
- **At its ceiling CI hits `MemoryMax` thousands of times with no kill;** a large `max` delta is reclaim, and its cost shows as stall seconds. `memory.peak` never falls without a reset; CI's reads above 18G from before the limit existed.
- **CI raise threshold.** Raise CI's `MemoryMax` only for an isolated run (one CI attempt alone in `hana-ci.slice`) with an `oom_kill`, or repeated isolated runs with sustained ceiling hits and at least 60 s of slice stall each, after checking minute coverage and build mix. Both runners share the slice, so an overlapping run is no evidence for a per-run raise. A changed limit needs another measured day after it.
- **CI's jobserver.** Inside a runner unit (`PrivateDevices`, `DevicePolicy=closed`) the pool device exists only with `BindPaths` and `DeviceAllow`; with an ACL alone `CARGO_MAKEFLAGS` yields a jobserver warning that hana's release job treats as failure.
- **sccache.** `sccache --start-server` forks and exits, so a service using it dies at start; with `SCCACHE_START_SERVER` in the environment every other sccache command fails, hence `ExecStart` only.
- **A step's own peak and stall exclude its compiles**, which run in `sccache.service`; the slice samples and snapshots see them.
- **sccache's compiles do not inherit a step's 500.** The server starts them in `sccache.service`, not in the step's scope; the unit's `OOMScoreAdjust=500` is what ranks them with the build steps; without it they sit at 200 with the sessions' processes.
- **Launch coverage.** `launches.py` globs one transcript level and misses launches in subagent transcripts (11 of 37 on the measured day). About a third of real launches lack `parameters.path`; `metadata.workspace` is only a folder name. `binary_path` appears for examples too, so only `metadata.launched_as` names an app. Counts are per pass; a launch count comes from stored `brp-launch` steps. Branch and SHA are collection-time facts. Transcript fixtures copy real lines.
- **CI re-runs.** GitHub copies every finished job into the new attempt under a new `job_id` with `created_at` at the re-run and the earlier stamps, which read raw gives a negative queue time and a duplicate duration.
- **Build-folder waits.** The `calls` table also holds port-lint calls, so every query over the call population, counted calls and token holders alike, filters `tool = 'verify.sh'`. Holders are fetched by `ended_at`, not `started_at`, so a holder that began the previous day is found.
- **Mac commands.** Tailscale SSH runs them under `/usr/bin/login`, which exits 0; judge the printed `rc=`. A sync pause never expires (the report's Mac line shows it), an unreadable pause file reads as paused, and `pause` can wait up to 30 s + 2 x 600 s for a running sync. `polled.json` is in `ci/` so sync carries it to the Mac; `sync_paused.json` stays local.
- **Disk.** send.py exit 1 (queued) counts as delivered. `disk.py`'s first walk is the cost (35 s under load 104). A file hard-linked into a target dir and elsewhere counts outside the caches. `floor.json` and `disk.json` hold only the latest record; past sweeps come from `journalctl --user -u disk-floor.service`. `sweep.py` reads `disk.json` by path and never imports `scripts/buildlog`, since `disk.py` imports `sweep`.
- **Disk floor order.** A no-op cargo build writes nothing under `target/`, and relatime refreshes atimes at most once a day, so only the use stamp records a step that compiled nothing. The stamp lags a lint step by up to 5 minutes (`invoke.sh`'s sweep rate limit), which only reorders targets used within the same 5 minutes. The build log's `sweep_freed_bytes` counts each workspace's budget sweep and the floor sweep after it together, so it is not the floor's cost alone; the journal's per-target lines are.
- **procps cuts user names** over 8 characters unless the format says `user:32`.
- **Codex.** On an idle thread the app-server opens a turn per queued message, so never `thread/queue/add` to a thread no launcher streams. `thread/read` can report active with no turn id; get the id from a steer probe before `turn/interrupt`. `turn/interrupt` errors when the turn ended in between; only a re-read tells that from a live turn.
- **Account reset times.** `resets` records the writing machine's wall time with its offset.
  Notes can move between machines with different zones.
  `note_file.reset_time` reads that value for account checks and alerts.
  For older offset-free values, readers use their own wall time.
  A naive `limit_reset` uses the writing machine's wall time.
- **`verify.sh` guard.** A failing `cargo metadata` exits with cargo's status under `set -e`, not 2.
- **Live scripts.** Units run `~/.claude/scripts/direct/board.sh` and `verify.sh` while they change, and bash reads a script as it runs: replace either by writing a temp file beside it, setting its mode, and `mv`ing it over.
- **A target with neither marker.** A folder made before a build that runs with `CARGO_CACHE_RUSTC_INFO=0` has no `.rustc_info.json` and no `CACHEDIR.TAG`; the floor and the disk measurement do not see it. Nothing on natedev sets that variable.
- **Cargo's tag is not only on targets.** `~/.cargo/registry` and `~/.cargo/git` carry the same `CACHEDIR.TAG` text, and other tools write the file with their own text; the tag counts only with cargo's line and a build tree beside it.
- **basedpyright** exits 0 when clean. Its summary reads `0 errors, 0 warnings, 0 notes`.

## Why

- **Admission by free memory, not by slice limit.** `memory.high` counts page cache and throttled builds that were not short of memory; `MemAvailable` is what earlyoom acts on. 12 GiB matches the floor steve already uses for slots, and a step's expected need replaces it once history exists, so parallel large builds no longer start together into memory only one of them fits in.
- **Target first below the disk floor.** Ordering every idle build unit by compile age spread each shortfall across the targets in active use, which rebuilt the output minutes later. One worktree's target is what a unit owns, so the least recently used target goes first and a target used minutes ago goes last. A forecast mode was ruled out: every build goes ahead, and the journal names whose cache went.
- **Scratch first below the disk floor.** A scratch target belongs to no worktree and is rebuilt only if its session is still working, so it goes before output a unit will need again. On 2026-10-08 a 140 GiB scratch target outlived managed targets idle for hours because it was used more recently, and a scratch target in its first build reached 22 GiB unseen, listed by the alert as growth outside build caches. No floor hook runs inside scratch builds: the 2-minute timer covers them, at about 2 GiB a minute against 300 GiB of headroom. A structural target rule (a child folder holding `.fingerprint` and a cargo lock) was rejected: one listing per walked folder for the no-marker case nothing here produces. Orphaned files still go first from every idle target, because losing one costs nobody a rebuild.
- **One slice for every session build, sccache included.** Every compile reaches the sccache server, so the server's cgroup is where compiles are charged; a limit on the step scopes alone would bound nothing.
- **CI in its own pool, a smaller slice and a lower kill order.** Shared steve slots alone did not stop kills (a CI run lost both Linux jobs after it). Its own 14-slot pool keeps CI from starving sessions and the reverse; `PIPELINE_JOB_OOMSCOREADJ` is the only setting that reaches job processes, because the runner sets every job to 500 itself. At 100 a CI job goes after a build step (500) or a session process (200) of the same size.
- **Build steps at 500, and no `--prefer`.** The level sets the order and size decides within it. A `--prefer` for compiler names (+300) let two hung hana tests (11.4 GB, 15.5 GB) outlive 26 smaller processes, six CI compilers among them; without it, a hung 15 GB test scores about 1100 against a 2 GB CI compiler's 750, and a session's rust-analyzer at 200 stays behind every build step of similar size. The adj is set inside the step's scope, so every process the step starts inherits it.
- **Admission after a hold release.** The 2026-10-04 incident had 21 kills after compilers grew beyond the admission readings. Each admitted step keeps its selected need. Its swap-backed use reduces its keep-aside. Every local build requests memory admission after a hold release.
- **A damaged record releases everyone, not no one.** Setting it aside and falling back to a broadcast cannot strand held sessions; each still passes its own memory gate.
- **Broadcast by id.** Names can belong to two sessions, and `top_level.py` once deduped them, so one of two same-named sessions never heard the hold.
- **A memory kill re-runs once.** The kill says nothing about the tree; a second kill stops the call so a starved machine does not loop.
- **zram over zswap.** A cache in front of a full swapfile adds no capacity; zram adds compressed space that does not depend on the swapfile.
- **Every average gets a p95.** An average hides the long runs that make waits; nearest rank needs no interpolation and matches the old p75 index for every n.
- **Two build-folder rows.** A worker's own verify.sh calls run one at a time, so its wait behind its own call costs nothing; only the wait behind another worker of the same worker session is delay.
- **Ruled out** (do not re-propose without new evidence):
  - systemd-oomd for builds: it kills a whole cgroup, and every session's compiles share one.
  - Admission by each crate's measured peak, or a per-crate gate: no source records the peak, and no isolated run met the threshold.
  - A nextest thread cap or a per-run memory cap in `verify.sh`: no isolated run met the threshold, and the widget drawn-pixel test group already bounds the largest step.
  - A `MemoryHigh` gate or `MemoryHigh` on either slice (see gotchas).
  - A `CARGO_BUILD_JOBS` fallback on the runners: the pool's jobserver reaches CI jobs.
  - A `steve-clients` group: the ACL already grants the runner users, and group membership is fixed at login.
  - Catching `AttributeError` instead of validating the cycle shape: it would hide code defects too. `release --resume` releasing the sole holder of any no-cycle hold: it could end a hold nobody had begun releasing.
  - A 10-minute snapshot search: an edge snapshot more than 2 minutes off misplaces the window. A per-call kill count alone: it cannot tell two single kills from one step killed twice.
  - Keying `ci_section` by job `created_at`: drops post-midnight jobs of a run started before midnight.
  - Counting build-folder wait that no same-session holder covers as the call's own: it would hide waits with no recorded holder.
  - A hook on `brp_launch` to record launches, or recording from inside bevy_brp_mcp: the collector reads transcripts instead. Appending launches to the host's monthly file: the per-launch file makes the record id the dedupe.
  - Keying the example guard on the target's `test` setting or advising `test = true`: examples carry no tests (user, 2026-10-04).
  - Withholding the phone push when a removal failed, or cutting a path's last component to fit 1,024 characters.
  - Queuing a message to an out-of-retries Codex thread (it opens a turn nothing streams); changing the service tier on capacity.

## Not built

`verify.sh ... --target-dir <name>` (a per-helper build folder with its own cargo token, pass records shared across folders, `calls.target_dir`, schema 10) is parked. It returns when the report's `Build-folder turn, behind another worker` row shows real time queueing for a worktree's build slot.

A second promise rule is measured and declined (showrunner, 2026-10-28: simple wins). The rule: a running build whose need is above a limit keeps its full promise; the running builds at or under it are promised the largest gap in full plus `max(0, 0.6 x need - anon)` each. The replay used 4 h of 5 s admission samples (2026-10-28 03:27-07:27Z, 240 waits, 1863 wait minutes), and "short" counts admitting ticks where the running builds' real growth after the tick passes `MemAvailable` minus the reserve. With a 6 GiB limit the rule saves 331 of 1863 minutes against 249 for the current rule, with the short rate at 0.8% against 0% (5 GiB: 326 min, 0.9%; 8 GiB: 501 min, 1.4%). The same rule limited by the waiting step's need never gets below 6.9% short. At about 4% of the wait for a second rule and a nonzero short rate, it is not built; a shortfall is the 2026-09-30 freeze. Do not measure it again unless the mix of steps changes.

A scoped reservation retains its accepted need while its scope contains a process.
Admission and queue views use the same lifetime rule.
The service retains `.max` and `.wave` until the owner and scope both end.

A test-width update persists the reservation in `state.json` through `os.replace`. A stopped write leaves the previous state intact.

A cleanup plan is written to a temporary file and linked into place only when no plan exists; a rerun over an incomplete older plan names the file and asks the caller to inspect and remove it before making a new plan.

Python queue layouts and evidence tables share `scripts/lib/terminal_cells.py::terminal_width`.
Combining marks occupy zero cells.
Wide characters occupy two cells.

**One decision pass.** `admission_pass(inputs, now, limits, policy)` returns decisions, blockers, proposals, the head cause, run keep-asides, and start spacing.
The service commits proposals and stores the pass.
It encodes View and Explain replies on the first request after each pass.
It keeps those bytes until the next pass ends.
Mine keeps the full reading and reuses the View encoding.
The protocol encodes each reply tree once.
A pending configuration still causes a pass before a query reply.
One reader thread collects process, scope, pressure, and memory evidence.
The reader publishes physical memory and sampler observations under a lock and wakes the service loop.
Each reading contains the monotonic time when its read started.
The loop applies the current peak hold and frozen test-phase need at each pass.
The reader applies no reservation policy.
A reading does not find the build run or the account of each process.
The pass finds them for the owners it checks, from the same process table.
One pass reads the build target list once, through `target_config.command_configuration()`.
The loop combines the latest reading with its current reservations and waiters.
A reading can be one pass cadence old.
The reader uses `waiting_pass_seconds` while steps wait and `running_pass_seconds` otherwise.
A change from the running cadence to the waiting cadence wakes the reader.
The service collects the first reading before it starts the serving loop.
The reader starts when an unusable configuration becomes usable.
An unknown owner requests an early reading.
A deferred request uses only a reading that starts at or after the request arrives.
An older reading leaves the request deferred.
The reader sends exceptions to the loop and continues at the configured cadence.
Direct test instances can supply a fixed reader.
Persistence reuses each unchanged record's encoding.
The state bytes, atomic writes, and lifecycle commit order stay the same.
`query_timeout_seconds` stays 0.5.
`QueueExplanation.decided_from` keeps the inputs for release calculations. Those calculations admit nothing and write nothing.
`apply_queue_priority` supplies the priority order. The generic codec stores domain records for replies and service state.

Admission callers retain one hash of their loaded admission sources.
`admission_code.py` owns `ADMISSION_CODE_PATHS` and `admission_code_version()` and includes itself in the hash.
The shell runs this module directly with isolated Python; the module imports only standard-library modules.
Requests carry `code_version`; the service records it by process identity in `ServiceState.caller_versions`.
Persistence removes dead caller identities, including reused PIDs, and retains live callers without reservations.
The shell retains its version in `BUILDLOG_ADMISSION_CODE_VERSION` before admission starts.
The drain check requires matching entry content and a matching recorded version; an unknown version prevents a completed drain.

### Priority and token waits after the service switch

The service owns the durable admission state and the socket.
`RankedWaiter` adds `PriorityUnit` or `NormalOrder` to a resolved row through `rank_waiter`.
A durable waiter has no `priority_rank` or resolved `order` field.
The order is priority, the step's original order time, then pid.
Token waiters keep their clocks and places but cannot protect room as the head.
`TokenWaitersOnly` distinguishes a token-only queue from an empty queue.

Priority accepts real roster units, including units without a waiting step.
The no-unit display placeholder is not a unit.
Unassigned sessions retain `NormalOrder`, including with an old placeholder priority record.
Unknown units return exit 3 with a stdout line starting `build queue: `.

`SettledPriority` and `PendingPriorityTransition` name the priority publication states.
A pending transition saves its log offset before the service publishes state.
Retries retain the transition id and read only the saved log tail.
The service imports old files once.
A failed cleanup logs its path and retains its record.
Each later finalization attempt uses the same guard, so other admissions continue.

### Mac disk floor

`sweep.py --floor-only --host mac` uses the existing floor scan and deletion order.
It reads `mac_test.conf` once for `sweep_floor_gib` and `sweep_floor_roots`.
The floor is 150 GiB. A missing floor or a floor below `free_floor_gib` refuses the pass.
The roots are `/tmp`, `~/rust`, and `~/.local/state/mac-test/mirror`.
Only targets with our use stamp, mirror mark, or a registered copy ancestor are eligible.
Resolved paths must stay inside these roots. The scan does not follow directory symlinks.

The sweep reads the local and natedev Mac claims under their existing admission locks.
Each reader releases its admission lock before it returns a claim snapshot.
The sweep holds acquired mirror leases through deletion.
It reads claims again before each removal group outside those leases.
A live or kept claim protects its mirror.
For a marked target without a copy record, the claim check compares each existing claimed mirror path.
A missing unrelated claimed mirror does not protect the target.
Other claim read errors still protect the target.
Remote mirror removal keeps its own lease exemption and still checks sibling leases.
An unreadable claim reply protects every target.
The claim server prints its reply after it releases the admission lock.
The Mac uses `lsof` for other process use. Unreadable process use protects every target.
The sweep checks protection again before each target deletion.
It takes only the shortfall, in the existing least recently used order.
The result line reports removed bytes, free bytes before and after, and skipped busy targets.
`--dry-run` lists proposed takes and removes nothing. It does not run the general tmp sweep.
The nix-darwin `nate.jobs.disk-floor` job runs this same command every 120 seconds on the Mac.
`modules/darwin/default.nix` imports the new `modules/darwin/disk-floor.nix` module.
The job uses mirror leases, short claim reads, and the shared floor lock.
It trims only the shortfall below `sweep_floor_gib`, without an offload request.
`ensure_mac_disk_floor()` keeps its 100 GiB on-demand trigger.

Nixos commit `efdf8bf` adds `/usr/sbin` after `/usr/bin:/bin` in the launchd job PATH.
The loaded folder-audit job includes that PATH at the 2026-11-08 inspection.
This PATH lets the floor, scratch and folder-audit jobs find the system `lsof`.
The floor job writes both output streams to `~/Library/Logs/nate-jobs/disk-floor.log`.
Nix-darwin activation deploys the job definition and its PATH.
The Mac floor finalizer uses the shared crossing path above and saves its receipts on the Mac.
Its session message goes to natedev; its phone title and body name `Mac`.
When a crossing delivery is pending, the Mac measures each root's direct folder children and each cargo target the floor pass found.
The target list contains only targets the Mac floor policy allows.
The Mac crossing alert waits for a pass that searched for targets.
A completed search with no targets still permits the alert.
Failure alerts do not wait for the target search.
The measurement skips targets that no longer exist and does not search the roots again.
Each size estimates the file blocks that deleting that folder alone frees.
An inode counts once only when every hard link is inside that folder.
An inode with a link outside the folder counts zero.
The measurement does not follow directory symlinks.
The alert lists these folders from largest to smallest.
It lists only enough folders to cover the shortfall, up to `sweep_alert_folder_count`, which defaults to three.
Held folder lines count toward this limit, but held bytes do not cover the shortfall.
Zero-byte folders do not appear.
Folder paths stay complete; home-relative paths may use `~`.
When the session text exceeds its limit, the alert lists fewer folders and does not shorten their paths.
If the full recommendation still exceeds the limit, the session text asks for measurements and names no folder.
The floor pass carries held target paths separately from folders with no recorded evidence.
Live process use, Mac claims, mirror leases, build holds, busy judgment locks, and held cargo locks identify held output.
A folder that contains held output, lies inside held output, or equals held output gets the held label.
The comparison uses resolved paths after home expansion and compares path components.
The held label takes priority over the protected label.
The alert recommends the largest folder without held output.
If that folder cannot cover the shortfall, the alert names its size and the partial result.
If every measured folder holds output, the alert says the sweep frees it when the build ends.
The session keeps the choice to delete nothing.
The measurement reads the Mac folders directly and needs no natedev disk snapshot.
The finalizer derives the machine name from its floor policy.
Unreadable folders can make the measured sizes incomplete.


### Free memory, head protection, and step slots

`memory_room` calculates room from one captured admission pass.
For each admitted run, kept bytes equal `max(0, selected need − swap-backed use)` plus held slot bytes.
Room equals free memory minus `safety_gib` and run keep-asides. The safety is 3 GiB.
The cap equals our swap-backed use plus CI use plus free memory minus safety. It does not limit an estimate.
The first two minutes use the same formula. Use and kept bytes never count the need twice.

The first eligible step follows queue priority, first run arrival, step arrival, and PID.
A younger run starts only if its need fits room minus the first run's need.
Pressure or unreadable pressure holds new starts and extra memory only while it lasts.
An unreadable pressure file shows `pressure unreadable: <path>` with a short error or `incomplete` when available.
The client and the queue explanation retain the pressure failure reason.
An unreadable host memory reading retains its recorded reason too.
If an unreadable reading holds queued starts for `reading_alert_seconds`, currently 300 seconds, the service sends one message to `natedev`.
The message names the path and the hold time. `natedev` owns machine recovery and any later phone alert.
The service measures the hold with its clock. A daemon thread sends the message without delaying admission.
The sender reuses `scripts.message.session_send.session_send_argv` with sender `build-admission` and sends the text through stdin.
The `build-admission-reading` claim prevents repeated delivery during the same unreadable stretch.
Failed delivery releases the claim for retry. Restored readings clear the claim.
The first run can start without sufficient room when no admission or held slot remains.
It still needs readable host memory, clear pressure, a current estimate, and due spacing.
CI and ownerless scopes do not count as admitted runs for this exception.

Each new start sets the next start time through `start_spacing_after`.
The interval is `recovery_start_seconds`, currently 5 seconds, for launches and build steps alike.
A completed step leaves its run a slot for `slot_gap_seconds`, currently 10 seconds.
The slot keeps the caller name from the reservation, including when a continuation claims the slot.
The snapshot decoder restores a missing caller name from the matching reservation.
If the reservation has no usable name, the decoder looks up the fixed staff ID in the supplied staff snapshot.
If the decoder finds no usable name, it supplies the label `unregistered session`.
The feed uses separate rules for caller names, step labels, and CI labels.
The client and service use the same formatter for wait names.
The formatter selects the unit, caller, or folder name in that order.
It adds the step when the step name is usable.
The client also accepts the head name from older replies.
The service and feed require a usable value for every field in a CI label template.
An incomplete CI template uses the generic CI label.
The feed omits unusable parts from step titles.
The wait subject joins the usable repository and step names. It shows `build` when both names are unusable.
The Builds page uses the names and hover titles from the feed.
A continuation within the slot bypasses spacing and pressure.
A larger continuation holds the slot and requests only its extra memory before new runs.
A closed connection frees its claim. An idle slot expires or ends with its verified run owner.
Repeated release cannot recreate a slot. A standalone request has no reusable slot.
Slots are not saved. Restart keeps active reservations and run order, but drops handoffs and waiting extras.

`queue_evidence.room_text` supplies the saved cap calculation to the Builds page and the memory report.
The Builds page shows zero room during a shortfall and records the shortfall in the cap explanation.
On 2026-11-21, Nate asked to “show the showrunner (in its color)” at 19:13.
At 19:17 he changed the row start to “only show the unit (and the showrunner in small text under)”.
The Builds page uses the same staff colors in the row and its hover.
Rows without a unit show their plain name and leave the small text and `Step` empty.
This includes Build tools, unowned Mac builds, compiler cache, unowned processes, and CI.
The small text is empty when the showrunner is the same name as the unit.
`Step` shows the step only when the feed's `title.unit` names a unit. When the step text is the unit, the feed sends an empty `title.unit`, so `Step` stays empty and the unit does not show twice.
`queueRowLabels` gives the row start, the small text, and the `Step` and `Verify` cells from one place.
For running rows, the table shows `Stage`, `Done`, `Dur`, `Verify`, `Step`, `Pid`, `Machine`, `vCPU`, and `Tokens`.
The waiting table uses `#` and `Waiting`, and omits `Tokens`.
The table has no `Unit` column and no `Slot` column. The feed sends no slot number; it sends only `slot_folder` and `slot_machine`, which the Disks page reads.
On 2026-11-23, Nate asked again for the Unit column to go because the row start shows the unit.
At 10:00 he asked why the Slot column still showed; natedev has no slots, so each of its rows showed `—`.
On 2026-11-23, Nate said: “where it is running should be what shows in the running column”.
On 2026-11-23, Nate approved the machine icons proposal of bob (List item 260): the `Machine` column shows icons, not names.
`MACHINE_ICONS` in `memory_live.html` gives each known machine its icon: lucide `server` for `natedev`, lucide `laptop` for `mac`, and lucide `cloud` for `aws`.
`mac-linux` is the laptop with a 🐧 badge. `hana-aws` is the cloud with a 🌸 badge. `hana-gpu` is the cloud with a ⚡ badge.
One hidden `<svg>` at the top of the page defines each symbol once, and each icon uses it with `<use href>`.
The badge is CSS text from the icon's `data-badge`, so it is not page text.
Each icon has its machine name as its accessible name (`role="img"` and `aria-label`).
A running row's `Machine` cell shows one bright icon in a faint box: the machine that runs the step.
The feed's `machine` field is the one source for that machine.
The row hover says `Runs on` with that machine, and lists the other machines that the step named under `Named targets`.
A waiting row's `Machine` cell shows one dim icon for each machine that can take the step, in the feed's order.
A waiting CI step has one target: the host machine, where the CI runners run (`memory_feed.ci_target_notes`). The running CI row shows the same machine.
Its hover names those machines in words under `Runs on`, after the `Waits for` reason.
A machine with no icon shows its name as text in its machine color.
An icon keeps the 20 px text line, so it does not make a row taller, and a row of icons never wraps.
Waiting rows size the `Step` column, so each step shows on one line; a new waiting row can move the columns to its right.
In a tight pane a waiting step breaks between words. There, the column takes the width of its widest word, so no word is cut.
Each word of a waiting step with more than one word is in its own `.word` span with `white-space: nowrap`, so a word such as `cargo-style` never breaks at its hyphen.
A legend under the table shows `Legend:`, then each of the six icons with its name.
Each machine on the page shows in its machine color: the `Machine` icons, the legend, the hover's `Runs on` and `Named targets`, Target availability, the Build limits headings, and the top costs.
The `.machine` rules in `memory_live.html` are the one machine color map. `config/memory_feed.conf` sets the colors for `natedev`, `mac`, and `aws`.
`mac-linux` is the Linux guest on the Mac, so it uses the `mac` color. `hana-aws` and `hana-gpu` are AWS boxes, so they use the `aws` color.
Every match in the map ignores case, so the hover words `Mac` and `AWS` match their machines.
The queued heading row has no bar. When the walk has room to the right of the table, the table moves right until the heading shows in full. The label column does not get wider for the heading. The `--queue-heading-gap` CSS property holds the gap between the heading and the table, and `fitWalkDetails` reads it. With no room, the heading ends in an ellipsis and its hover gives the full text.
`Verify` shows the Rust verify verb when the feed records a Verify call.
`Step` shows the feed's `title.step`, which uses the build step names from `config/build_steps.conf`.
The hover shows the starting session under `Caller` and the running row's detail under `Detail`.
The first line of the name, the progress bar, and the first line of each column share one center line.
The showrunner and memory amounts stay below that line.
The first column shows `building` or `testing` from the recorded progress counter.
A natedev row shows the stage `waiting` while its nextest step waits for test tokens; its `Done` cell shows `—`.
The row tooltip gives the full reason, `Waiting for jobserver tokens`.
`progress_profile.token_wait_line` prints the wait line in `nextest_tokens.py`, and `CounterParser` reads it from the step log as `NoCounter.token_wait`.
The wait ends at the next line, which reports the tokens taken.
`NoCounter.token_wait` is an optional field, so the counter wire union does not change for pinned clients.
The admission reading stores it in the running step's `measured.counter`.
A row without a counter shows `—`.
Local builds, merge gates, and cloud rows use their recorded counters.
hana CI currently has no admission counter.
The Running table shows hana CI only when a recorded runner has a busy job.
Idle or unreadable runners have no separate Running row.
Their memory remains in the taken total, the cap, and the summary tooltip.
Waiting rows use `#` in the first column for their queue place.
Both sections use the same column widths.
The table is always beside the chart, and each step uses one row at every pane width.
In a narrow pane the table narrows its column gaps.
A pane still too narrow for the chart and the table scrolls the Running and Queued card sideways; the page itself does not scroll.
The card never scrolls down, and its axis labels fit their 16 px row.
The table starts half the cap line and one gap past the cap, and the fit measures the table at that same place.
The label column is as wide as the widest row title or small line, running or waiting, with the reorder handle space of its row. The Free row also counts.
When the queue shows reorder handles, every waiting row keeps the handle space, so the title of a fixed row lines up with the others.
A row title never wraps. In a pane too narrow for it, the title ends in an ellipsis, and the row hover gives the full text.
A small line under a title (the showrunner or repository line, or a waiting note with the job start of a CI row) never wraps either. It ends in an ellipsis in the same way.
The 12rem cap (9rem in a narrow pane) on the label column stays.
The chart, the top bar and the axis start one even gap after the label column.
The table room row is empty and only keeps the rows aligned.
`memory_page_proof.mjs` fails when the table leaves the chart's side, a Stage or Step cell is cut, or the label column is wider than its labels.
It also fails when a `Machine` icon has no machine color, or the `mac-linux` icon does not show the `mac` color.
It checks the queued rows at 1440 px, at 900 px, and in a tight pane at 800 px.
It fails when a running row does not show one bright icon with its machine name, or a waiting row does not show one dim icon for each machine that can take it.
It also fails when a row of icons wraps, the legend does not show the six machines, a row title takes two lines, or a Step cell is cut.
The other checks hide scrollbars. One more load at 1440 px shows them, and the proof fails when the Running and Queued scroll box shows a scrollbar or can scroll.
`--queue-rows` also saves that card's right edge as `after-edge.png`, or `before-edge.png` for a before page.
`checkStandingOrders` in `memory_page_proof.mjs` checks nate's standing orders for the Builds page (`docs/plans/builds-page-standing-orders.md`).
It runs in the gate proof, and alone with `--standing-orders [page]`, which also saves `real-1440.png` and `real-900.png`.
The optional page must be a rendered page, such as the `memory_live.html` that the proof writes in its folder. A source page does not load.
It loads `scripts/build_queue/builds_page_real_rows.json`, a cut of a real queue reading. It has these rows:
1. A hyphenated unit, and a unit with no staff entry (`lfo`).
2. Showrunner runs with no unit, one with a long step.
3. A running and a waiting hana CI row.
4. A Mac build, and an AWS row.
5. A fixed Mac-only waiting row.
6. A waiting hana CI row with a long waiting note and a job start.
The queue-move stub gives the page a token, so the reorder handles draw.
At 1440 px and at 900 px, dark, it fails when one of these is false:
1. The table has no Unit and no Slot column.
2. A unit row starts with its unit in its unit color, with the showrunner line under it. A row with no unit starts with its owner, and its title has no `: `.
3. A showrunner's own run starts with the showrunner in the showrunner color, and has no small line.
4. Each row title has one line, and each small line under it has one line. At 1440 px no title ends in an ellipsis. No line of a Step ends in `-`.
5. The table text and the machine icons are in line with the first line of the row title, within 2 px.
6. Each movable waiting row has a reorder handle, and the handle does not cover the title. A fixed row has no handle. All waiting row titles start at the same left edge.
7. A running row shows one bright icon for its machine. A waiting row shows one dim icon for each machine that can take it. The hover names each machine.
8. A Mac-only waiting step has no memory box, because the chart shows natedev memory.
`test_memory_feed.PageTextTests.test_the_page_and_its_fixtures_use_only_configured_job_names` fails when `memory_live.html`, the page fixtures or the page tests show the retired job name "merge gate". Job names come from `config/build_steps.conf`.
The red cap line in the Running chart stops at each line of chart text that it would cross, so no text shows on it.
`cutCapLine` draws the line as parts after the bar labels are placed; text clipped by an ellipsis does not count.
`memory_page_proof.mjs` fails when chart text shows on the cap line or the table room row shows text.
The Mac reader reads process data and cargo tile registrations in one SSH call per refresh.
It uses cargo tile's `/tmp/cargo-tile/<uid>/state/pids` records and their named log files.
A v3 registration must match the current boot, process start time, uid, PID, and generation.
The reader reads at most 64 KiB from each registration and log.
It groups descendant cargo calls and compilers under their top-level cargo run.
Native projects and both test mirrors appear as separate runs.
The recorded project directory identifies the run; a compiler's crate directory does not.
The step name uses the command's package, then the repository when the command has no package.
A step name never contains an unknown-value dash.
The reader also reads the repository URL from the workspace manifest when the Git origin is absent.
A Mac row with a unit starts with the unit, with the showrunner under it.
The Mac reader records the launcher of each cargo run. It walks the parent process chain of the run and takes the first program that `queue_view.MAC_LAUNCHER_PROGRAMS` names:
1. `claude` is a Claude session. The session id comes from the run's `CLAUDE_CODE_SESSION_ID`, then from the session marker in the copy record.
2. `Runner.Worker` or `Runner.Listener` is hana CI. The CI origin uses the same walk.
3. `rust-analyzer` is rust-analyzer.
A program name can have a version suffix, such as `rust-analyzer-2026-11-02`.
A session marker with no launcher program is a Claude session. A run with no launcher program and no marker is `terminal`.
`memory_feed.mac_launcher_name` gives the shown launcher: the staff name of a known Claude session, `Claude session` for an unknown one, `hana CI`, `rust-analyzer` or `terminal`.
A Mac row with no unit starts with its owner, from `owner_name`. A known staff owner comes first, then the launcher. The repository is the small text, and the step is in Step.
The staff name shows in its staff color; the other launcher names show in plain text. The row hover names the owner in a `Started by` field.
A reading from before the launcher field has no launcher, so its row keeps the old name that starts with the repository.
A placed Mac row shows only its step name; the placement id stays in the row's `gate_label`.
A `user`, `hana-ci` or `unplaced` run adds that label to its name.
The stage uses a test counter or a running test binary as evidence for `testing`.
The reader resolves the target folder from Cargo arguments, the process environment, and Cargo configuration.
It embeds the shared Cargo folder reader in the same Mac reading.
The Mac kernel supplies only the selected Cargo path environment fields.
Observed compiler output folders also identify test binaries, as they do in cargo tile.
The reader resolves symlinks before it compares directory components.
A test binary under a custom target folder can supply `testing` without a counter.
Build scripts and binaries outside the selected output folders do not supply `testing`.
The stage otherwise shows `building` for an observed cargo run.
The reader uses the same captured Cargo and nextest output that cargo tile reads.
The page shows the recorded done percentage and a progress line.
The command's `--test-threads`, `-j`, or `--jobs` value supplies vCPU.
An absent positive value supplies `—`.
The Unit comes from the submitting session in a matching Mac claim and that session's staff record.
A remote mirror session and repository can identify that claim.
A mirror lease can identify the claim session.
The copy record's fallback owner does not supply a Unit.
The page shows `—` when no claim supplies an owner.
The Mac row uses the local used-memory style and the same GiB scale.
Its memory is the sum of resident memory for the run's process tree.
The Mac row has no reservation or hatching and changes no natedev memory total.
The Mac tooltip states that its memory does not count against natedev.
It aligns Machine, Started, Used, and Reading age in labelled rows.
A readable counter adds a Progress row.
A failed or old reading replaces the Mac run rows with one short `Mac builds` status.
The feed log retains raw errors; the page shows only the status and reading age.
CI runner records supply the `hana CI` owner for their Running and Waiting steps.
The aggregate running CI row also shows Unit `hana CI`.
The feed compares the step's recorded runner unit with the runner records, not page text.
The terminal queue view shows a `machine` column after `min` and before `pid`.
The column uses `scripts.buildlog.store.host_name()`, the shared short host name.
A narrow view omits the machine column before it reduces name space.
Waiting rows have no machine value. Services lines do not change.
The admission service admits each running step on its own machine. No per-row protocol field is added.
`keep_aside_text` explains each admitted run's need, swap-backed use, slot bytes, and kept bytes.
`RoomCheck.basis` records `Fits`, `StartsAlone`, `WithinSlot`, or `DoesNotFit` in the admission walk.
`admission_pass` records one `HeadCause` on `HeadAdmissionStatus`.
`HeadExplanation` retains that cause for the view and release advice.
The formatters read the captured cause and room without a second room calculation.
No adaptive controller, fixed admission limit, order window, CI growth promise, or test-phase estimate worker remains.

### Progress memory profiles

Merge gates start with a ranked peak from their host's recorded peaks from the last 14 days, as other programs do.
Without history, they use `program_first_peak_gib.gate` from `config/build_admission.conf`.
Matching progress profiles still determine the running need.

Cargo build, check, clippy, mend, nextest, and merge gates record attributed swap-backed use in five-percent phase bins.
Progress bins and program peaks use one ranked selection rule.
For count `n`, the rule selects the Nth-highest value, with `N = min(n, max(floor, n * share // 100 + 1))`.
`peak_history_share_percent=10` sets the share in `config/build_admission.conf`; its whole-percent range is 0 through 50.
Progress bins use `progress_profile_rank=2` as the floor; program peaks and nextest phase peaks use floor 1.
At a 10% share, floor 1 gives the previous p90 result, including counts divisible by ten.
A count below the floor selects the lowest available value.
The service checks the rank and share before each pass.
It rebuilds merged profiles when the rank or share changes.
The service applies each step's recorded headroom rule to the highest selected bin within its look-ahead window.
`progress_lookahead_percent` is read at use; its initial value is 20.
The window crosses Cargo phase boundaries, including repeated build phases.
The merge gate window stops at the end of `suites:1`, even when history contains rerun phases.
Reruns without a measured window retain the full need. Each step has its own history key.
Missing counters, profiles, or anonymous readings retain the full need.
Each saved record names its peak reading source with `ScopeReading` or `ProcessTreeReading`.
`ScopeReading` stores the scope directory. `ProcessTreeReading` stores the process ids.
At each new run peak, the service records the phase, bin, anonymous bytes, and source processes.
Each `ProfileProcess` stores its pid, command arguments, and `RssAnon` bytes.
Scope evidence includes all recorded scope members, including helpers outside the original process tree.
The service reads process commands and `RssAnon` only for a new highest run value.
Lower samples, equal samples, and lower bin peaks retain the previous process evidence.
Active records retain this evidence across service restarts and completion.
The history merge reads only each record's profile and time; it does not keep the process evidence in memory.
Each pass reads stale histories for at most `progress_profile_sweep_seconds` (initially 0.05), and always reads at least one key.
After a restart, the service reads the history of each running step's key at once, and the other keys over the next passes.
The sweep also deletes expired records, so an expired record can stay on disk until the sweep reaches its key.
Old records remain readable with `ReadingSourceNotRecorded` and `PeakProcessesNotRecorded`.
Unavailable process commands and anonymous readings have named states with their read errors.
The nextest test phase keeps its frozen baseline and margin as the fallback. Its live test peak remains a floor.
Registered CI steps supply counters and profiles through admission.
`lint/invoke.sh` enables Cargo progress in its capture log. It removes redraw frames from worker output.
Cargo-tile enables progress for its own captures. The cargo wrapper does not enable progress for other pipes.


### CI cargo admission

The pinned client entry is `scripts/build_queue/ci_cargo_front.py`.
The runner package exposes this entry as `bin/cargo` from the pinned source tree.
The package wrapper passes `--front "$0"` before the cargo arguments.
Its PATH entry precedes the cargo-tile shim, which precedes the real cargo.
The front excludes that wrapper and removes its directory from the child's PATH.
An explicit `--cargo <shim> -- <arguments>` entry also selects the next shim.
Building calls hold one reservation through `queue_place.held_reservation` until the child and its descendants end.
A grant without runner timing is released at once, and the call exits 1.
Non-building calls execute the next cargo without admission.
A direct call to a cargo executable outside this front bypasses admission.
Calls inside the admitted child tree retain the same reservation.

`ServiceEstimateRequest` carries the owner, repository, worktree, arguments, runner, job, and job start.
The service reads configuration and history with its own account.
It uses `step_estimator.step_estimate` with a recorded repository location.
The service refreshes the estimate on each acquisition retry.
Build estimates use the current service history generation.
CI uses `NoRun`, so it claims no run place or continuation slot.
The configured `ci_label` expands `RUNNER_NAME` and `GITHUB_JOB`.
The configuration reader validates the template before the service uses it.
An invalid template uses a committed value through the normal configuration reader.
Without a usable committed value, configuration resolution fails as for other invalid values.
The queue records this label as the session name.

`SO_PEERCRED` supplies the connecting account before replay lookup.
Authorization also precedes caller version records and saved state changes.
Runner request ids start with `uid:<account>:`.
The service checks the owner's account and process start time.
It also checks a caller-named run's account and process start time.
Runner accounts cannot change priority or admit the head.
Cleanup and width changes require a reservation from the same account.
The account prefix also binds replay and pending priority access.
Saved reservation and ledger records retain their existing shapes.
The service wraps saved grants as `RunnerAdmitted` only when it sends them.

The runner owns its child process tree; it creates no user-manager scope.
The front adopts orphaned descendants before it starts cargo.
The cargo child sets a parent death signal before exec.
SIGKILL of the front also kills that cargo process.
Cargo descendants do not inherit that signal and can survive a SIGKILL of the front.
`QueuePlace` tracks descendants and terminates surviving descendants before release.
The runner samples swap-backed use in its process tree.
The service also samples swap-backed use during each admission pass.
The service reads the runner's peak and wave files beside its sidecar.
It copies the maximum into its own peak files before finalization.
The service writes completed and failed peaks through the normal history path.
The runner creates its sidecar and progress counter in a temporary directory, below `--shared-folder` when that argument is given.
The directory mode is `0755`; the sidecar, counter, peak, and wave modes are `0644`.
The runner account owns these files; the service reads them without group write access.
The service completes release when sample deletion fails with `PermissionError`.
The front removes its temporary directory after release.
A front killed by SIGKILL leaves that directory for the existing sweep, or for the `systemd-tmpfiles` age rule of a `--shared-folder` folder.
The service reads the counter through the sidecar path in the reservation.
The child alone receives `CARGO_TERM_PROGRESS_WHEN=always` and `CARGO_TERM_PROGRESS_WIDTH=80`.
The shared progress reader and profile history apply the existing look-ahead rule.
A missing counter or profile retains the full need.

Acquisition uses named protocol timing without a local configuration read.
The deadline covers closed connections and connections without replies.
A valid reply renews the deadline.
Cleanup uses the separate timing from the grant.

`job_start` walks up the parent processes of the front to the `Runner.Worker` ancestor.
The runner starts one `Runner.Worker` process for each job, so its start in `/proc/<pid>/stat` is the job start.
`process_stat.name_and_fields` reads each process name and stat fields.
A parent cycle in `/proc` ends the walk without a job start.
Each building call reads its job start once, in CLOCK_BOOTTIME seconds.
When `GITHUB_ACTIONS=true` and no `Runner.Worker` ancestor is readable, the call prints one `::warning::` line on stderr.

A CI step keeps the queue place of its job start.
GitHub counts its 6-hour limit for each job from the runner pickup, and the `Runner.Worker` start is that pickup.
The job counts, not the workflow run.
The front converts the job start once to the queue clock with `queue_job_start`.
It reads the queue clock and CLOCK_BOOTTIME at the same moment and subtracts the boot age of the job start from the queue clock.
`ServiceEstimateRequest.job_start` carries `JobStart(started_at)` or `NoJobStart`.
An older client sends no field, and the service reads it as `NoJobStart`.
The service copies a `JobStart` into the waiting row as `ci_job_start` and into the saved `WaiterPlace`.
The service sets `order_since` to the earliest of the step arrival, the run place, and the job start.
So each step of one CI job ranks at the job start, ahead of steps that arrived after the job started.
Queue priority remains the first ordering field.
A step with `NoJobStart` keeps its arrival place.
`state.json` saves the job start with the waiter place, so the place survives a service restart.
The Builds page waiting row shows `job started <time> · <age> ago` in the user zone, after any wait reason.
Its hover shows the same time and age under `Job start`.
The page shows no time left to the 6-hour limit; that comes with a config key for the limit.

Registered process trees count through their reservations.
The CI slice reading subtracts their swap-backed use.
Unregistered CI use counts once in the cap and has no memory reservation.
A CI job that admission cannot cover uses the systemd `MemoryMax=34G` limit of 34 GiB.

NixOS activation remains natedev's work.
The socket uses group `build-admission` and mode `0660`.
The group includes natepiano and the hana runner accounts.
Runner sandboxes need `BindPaths=/run/build-admission` when `/run` is hidden.
`ADMISSION_SOCKET` is needed only for a nondefault socket path.
The package must contain the pinned source tree and a Python interpreter.
No client code is copied into `/etc/nixos`.
The service must read runner `/proc` records and runner temporary counter files.
`nix develop` sets `TMPDIR` to a private `0700` folder, so `TMPDIR` cannot name these files.
The front takes `--shared-folder <folder>` for them and leaves `TMPDIR` to the job.
The folder is `/run/hana-ci-admission/<runner>`, with mode `0755` and the runner's ownership.
It stays outside the socket's runtime folder.
The socket sets `RuntimeDirectoryPreserve=yes`, so a socket restart keeps the folder that running runners bind.
The wrapper runs the front with `python3 -I` and the script path, and exports no `PYTHONPATH`.
The fixed slice limits remain `MemoryMax=34G` and `MemorySwapMax=2G`.
The service authorization and protocol must be live before runner socket access starts.

## Test selection and fresh-build evidence

`scripts/build_queue/step_estimator.py` owns the test-history selection and freshness checks.
`verify_test_selection` returns `PackageTestSelection(package, filters)` only for the whole positive expression that verify emits.
It accepts a package with one `test()` predicate or a union of those predicates.
A bare package expression selects a full package run.
Every other expression gives `BroadTestSelection`.
Negation cannot supply an exact filter key.
`test_selection` parses package, scope, and canonical sorted filters together.
A package-less filtered run keeps broad filtered history.
Waiter estimates reuse a selection only when package and filters match.

`filtered_history_peak` uses the larger exact-filter and package-filtered anonymous p90.
It then tries indexed exact-filter peaks, indexed package peaks, and package check/build compile peaks.
Indexed peaks use `ANON_SHARE`.
Without usable compile history, it uses `FILTERED_FLOOR`, which is 1 GiB.
A full-suite estimate does not cap this compile estimate.
The recorded package, filters, and scope take priority over values recovered from argv.

`is_fresh_test` reads the last completed nextest step for the same worktree, package, and scope.
That step must have the same `test_build_key` and either status 0 or a positive test count.
It must end less than 24 hours before the estimate.
Both Cargo output roots must still exist.
No matching deletion record or later sweep with unknown or positive freed bytes may invalidate them.
A read error, malformed JSONL tail, missing key, or unknown output roots rejects freshness.
`current_worktree_steps` uses an index prefix only after validating its file marks and then reads the JSONL tail.
The index cannot hide a newer step.

`fresh_test_peak` uses completed zero-compile measurements from the same package and scope.
It tries exact-filter anonymous and indexed history before package history.
It joins anonymous rows by call id and keeps the 1 GiB floor.
`NoFreshHistory` returns the estimate to its compile-history path.
Fresh waiters receive another estimate at each check.

`TreeInputs.keys` writes the build key and both output roots through `VERIFY_TEST_BUILD_INPUTS_FILE`.
The key excludes run-only filters and result-environment values.
It retains Cargo inputs and `NEXTEST_CARGO_PROFILE`.
`record.py` stores `test_build_key` only when source stays unchanged during nextest.
Rows without a key cannot prove a fresh build.
The broader `BUILDLOG_TREE` field cannot replace this key.

The merge gate and style lint own private folders through `build_folders.temporary_build_folder`.
The contexts remove these folders after success or an exception.
Style review keeps its input folder for the review worker and finish command.
Session TMPDIRs and unit session folders remain for later session commands.


## Synchronous verify helpers

`verify.sh` shares one Python process across synchronous helper calls.
See [Synchronous verify helpers](build-targets.md#synchronous-verify-helpers) for the protocol, process exceptions, recovery, and trace format.

### Move a waiting step

A drag acts like a bump: main's claim rules run on the shared order.

Use the three-line handle on the Builds page to move a natedev wait to any place in the list.
To use the keyboard, press Space on the handle, press an arrow key, then press Enter.
Press Escape to cancel.
The numbers show queue positions.
The policy still checks eligibility before each start.
Cloud waits have their own order.

The admission service gives each wait an ID when it enters the queue.
The saved place binds the ID to the owner PID, process start time, original arrival time, and episode.
A refresh keeps the ID.
The service saves pins in `state.json` when a move completes.
A pin records the wait ID and an anchor.
The anchor identifies the row directly ahead or the head of the queue.
The service applies the usual priority and arrival rules to all saved waits.
It then places each pinned wait directly after its anchor.
A move gives the moved row a new anchor and keeps its former successor in place.
Absent and unnamed waits stay in the full order.
The page omits waits with no name source.
Placement, memory admission, and continuation claims use the eligible rows in this order.
The head-stall snapshot uses the order of local waits.
The grant pass shows a newly granted row in Waiting without a move handle.
The first pass that counts its reservation shows the row in Running and includes the memory charge.
When run times tie, the earlier step arrival comes first. PID breaks a tie in step arrival.
A confirmed move or Undo changes queue order only.
A drag can return a waiting row's unstarted slot claim, as a bump does.
A departing row loses its pin.
A row anchored to the departing row inherits its anchor or its predecessor in the full order.
A local reservation keeps its saved place until the service sees processes in its scope.
The first reading clears the saved place for a direct reservation without a scope file.
A placement retry before launch keeps its place.
Both hosts use the visible order for normal steps.
When placement preparation sees a remote start, it ends the startup hold and removes the local waiter.
The table includes remote-only waits in queue order.
The table identifies waits that a build hold blocks.
Speculative Mac copy rows show the newest-first rule.
A speculative Mac copy waits for placement outside the table while its parent holds local memory.
A refusal before launch keeps the pin. Closing the attempt removes the local waiter and drops its slot claim.

The service checks the order revision for all rows, including rows without handles.
The replay ledger records the reply and the anchor from before the move.
Undo restores the step to that anchor in the full order.
The anchor is the head or the row that directly preceded the step.
If that row left the full order, the service refuses Undo.
Undo saves pins as a new move.
A conflict supplies the current order. If the revision changed, the page waits for a fresh reading.
A move sends the drop target.
A drop above the first shown row uses the head anchor, ahead of unnamed and absent waits.
Other drops use their neighbor in the full order.
An Undo request identifies the move to restore. The service reads the saved anchor from the replay ledger.
The page applies the confirmed order to the rows it shows.
The confirmation uses the table numbers, including the places of waits without handles.

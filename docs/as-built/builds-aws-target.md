# AWS cloud gates and trials

## Current model

AWS runs temporary Spot boxes for merge gates, paired AWS trials, and the GPU size trial.
Normal build jobs use `mac` and `natedev`, in that order.
AWS has no permanent builder or build offload route.
Cloud gates use `~/.local/state/build-targets/aws` without an `aws` target registration.
`regions` in `cloud_gate.conf` holds all 19 regions of `spot_candidate_regions`.
The saved deployment record identifies the regions available for launch.
Boxes boot the official NixOS image and fetch their tools at boot.
GPU trial boxes then import the prepared GPU system and reboot.

`config/aws_target.conf` supplies the account spending caps and billing settings.
`config/cloud_gate.conf` supplies launch, expiry, trial, and artifact settings.
The ledger reserves spending before launch and records confirmed termination.
The configuration files remain the source for cap values.

`report.py` reads the AWS state folder without target registration.
Its AWS section retains the account bill, daily gate spend, and historical builder comparisons.
`sync.hosts.aws=aws-build` identifies historical steps.
The report shows no synchronization status for the removed builder.

One resolver selects the scaler credential file for deployment, services, key creation, and teardown.
It uses a nonempty `AWS_SHARED_CREDENTIALS_FILE` override before `credential_file` from `cloud_gate.conf`.
It expands `~` in either path.
Deployment uses encrypted administrator credentials for resource changes.
Gate services use the private scaler profile.
Boxes receive signed URLs and no scaler credential file.

## Hana guest safety files

`scripts/aws_target/hana_guest.py` renders safety files for the planned kept Hana box.
These files do not provision a machine or add a deployment command.
Phase D must install and enable the units before the box accepts work.
`config/hana_aws.conf` keeps placement disabled and supplies all safety timing and resource values.

`scripts/aws_target/hana_render.py` renders both the safety files and the receiver files from this repository's configuration.
It writes nine files by base name and applies each `GuestFile` mode.
Without `tool_setup`, `hana_guest.render_files()` omits `hana-tools.service`.
Phase F2 supplies the tool setup service.

The NixOS build uses the renderer from its pinned `claude-commands` input.
`scripts/aws_target/hana_render.files` lists the files for the NixOS `lib.fileset` filter.
The filter excludes unrelated repository changes from the image inputs.
The NixOS integration replaces the manually copied guest files.

Run this command from the repository root:

```sh
python3 -E -m scripts.aws_target.hana_render --device /dev/sdf <out_dir>
```

`-E` ignores `PYTHONPATH`, so the listed files alone supply the imports.

The boot guard waits for a fresh permit within `guard_grace_seconds` from boot.
It has no ordering or requirement dependency on the data disk, network, or tool setup.
Tool setup runs after the guard and requires the bounded data mount.
A failed setup or missing disk cannot prevent the guard from requesting poweroff.
The mount bounds both device discovery and mount execution through `mount_timeout_seconds`.

Phase D must deliver `/run/hana-aws/work-permit.json` and `/run/hana-aws/generation` as private, root-owned files.
The permit fields are `expires_at` (UTC epoch seconds), `generation`, and `boot_id`.
The boot ID must match `/proc/sys/kernel/random/boot_id`.
The guard and watchdog only read the permit and identity files.
Missing, unreadable, malformed, expired, or mismatched permits grant no permission.
`permit_max_seconds=1800` bounds one renewal window.
A permit beyond that window grants no permission.
Phase C reserves permission for longer steps.
Phase D must deliver renewals through `write_permit()`.
The writer uses a private temporary file and atomic rename.

On the first read, safety records the permit's boot-clock deadline in `/run/hana-aws/permit-deadline.json`.
An unchanged permit retains that deadline across later reads and service restarts.
A renewal with a changed expiry records a new bounded deadline when safety next reads it.
The watchdog stops at the earlier of wall-clock expiry and the boot-clock deadline.
A backward wall-clock change cannot extend the boot-clock deadline.

Safety, permit renewal, and command admission share `/run/hana-aws/permit.lock` through `fcntl.flock`.
Safety holds the lock from the permit read through the shutdown decision.
Before killing work, safety writes `/run/hana-aws/shutdown.json` under the lock.
This fence refuses later renewals and command admission for that boot.

The independent watchdog starts after the guard and reads no controller or command state.
At expiry, it sends SIGKILL to all command cgroups under `hana-work.slice`, then requests poweroff.
A failed kill request still triggers the poweroff request.
The watchdog checks at `watchdog_check_seconds` intervals and shortens its next interval to the permit deadline.
Systemd also requests poweroff if a safety service fails.

`admit_command()` requires enabled configuration, a fresh bounded permit, and a mounted data disk.
It checks the shutdown fence and starts the command under the shared lock.
Its start callback spawns the process and returns immediately.
The caller waits for command completion outside the lock.
It prepares an unprivileged command with a separate slice under `hana-work.slice`.
Both the parent slice and each command enforce `step_cpu_limit` and `step_memory_gib` from configuration.
The initial limits are 4 vCPUs and 24 GiB RAM.
Phase F2 must use this command path after checking the actual data mount.
`test_hana_guest.py` tests the decisions with injected clocks and verifies rendered units when `systemd-analyze` exists.
No safety test needs AWS or a running systemd manager.

## Hana spending permits

`scripts/aws_target/hana_spend.py` reserves spending before it returns a guest permit.
`HanaSpending` uses the account ledger lock for starts, renewals, lifecycle observations, and storage deletion.
It makes no AWS call and delivers no permit to a guest.
Phase D must deliver permits and schedule lifecycle reconciliation without an active caller.
Placement remains disabled.

`reserve_storage()` reserves the root and data volumes before provisioning.
It checks current account caps and the Hana budget before returning a saved reservation.
`issue()` reserves compute through the deadline at the configured bid ceiling.
It includes retained storage when no storage reservation exists.
Each issued permit has an increasing generation.
A renewal must name the current generation and extend an unexpired deadline.
A refused renewal leaves the saved deadline unchanged.
The guest watchdog then stops the box when its existing permit expires.

The shared ledger checks account policy, account evidence, gate exposure, bills, and the existing daily and monthly caps.
The account daily limit remains USD 35.
The Hana daily budget includes Hana compute and storage within that account limit.
Open compute exposure remains reserved after permit expiry until a lifecycle observation confirms `stopped` or `terminated`.
Storage remains reserved after stop and termination until volume deletion is confirmed.
Storage estimates use a 30-day month.
The ledger reserves the remaining storage cost for every day that a permit reaches.
`reconcile()` records an automatic restart even when Hana placement is disabled.
It reserves the boot guard interval and retains exposure until a confirmed stop.
Unknown lifecycle state releases no exposure.
Each lifecycle observation names the generation that the observer read before its request.
An observation for an older generation changes no exposure or observation time.
The ledger saves `hana_lifecycle_fence` with the latest accepted observation or permit issue time.
An older observation cannot close a newer permit or replace a later running observation.
The fence survives controller restarts.
Observations at the same time as the fence leave exposure unchanged.

The existing `cost-ledger.json` version remains 1.
The optional `hana_compute` and `hana_storage` collections hold Hana exposure records.
Older records have no Hana exposure when these collections are absent.
A missing `hana_lifecycle_fence` remains absent until a fresh observation arrives.
The first stop confirmation saves the missing fence and retains the compute exposure.
A newer confirmation for the same generation can then close that exposure without a running observation.
Missing fields within a Hana exposure record make that record unusable.
The reader never invents a zero price or deadline.
Account guards and gate reservations include Hana exposure.
Ledger estimates include accrued Hana compute and storage.

| New setting | Value | Reason |
| --- | --- | --- |
| `region` | `us-east-2` | Use Ohio for the box and retained storage. |
| `instance_type` | `m7a.2xlarge` | Provide 32 GiB of memory. |
| `spot_max_usd_per_hour` | `0.15` | Bound reserved compute exposure. |
| `root_gib` | `40` | Reserve the retained root volume. |
| `data_gib` | `300` | Reserve the retained data volume. |
| `daily_budget_usd` | `6` | Bound Hana spending within the account limit. |
| `gp3_usd_per_gib_month` | `0.08` | Price standard gp3 storage without extra performance. |

The account configuration has storage totals, but no shared gp3 price per GiB.
The new rate therefore belongs to the Hana configuration.
`test_hana_spend.py` uses private files and fake lifecycle observations.

## Hana lifecycle controller

`aws_target.py serve` supervises bounded `tick` child processes for the existing `aws-target.service`.
Each child attempts Hana lifecycle recovery before account charge refresh.
Either operation can fail without suppressing the other operation.
The supervisor continues after a failed or timed-out child.
It releases all child-owned locks when the child exits.

`hana_box.HanaController` stores its journal in `hana/lifecycle.json` beneath the account directory.
`controller_lock.py` holds the existing `controller.lock` implementation.
`aws_target.control_lock` remains available through the same import.
Claims, lifecycle decisions, and operation intents take that lock before any ledger lock.
The controller releases both locks before each provider call.
It checks the saved deployment identity and operation token before applying the response.
Each tick performs at most one provider step.

The journal stores these lifecycle states:

| State | Meaning |
| --- | --- |
| Unprovisioned | No setup has registered a deployment. |
| Idle-stopped | A deliberate idle stop permits a later demand start. |
| Interrupted | Only EC2 can restart the interrupted Spot instance. |
| Stopped-unknown | An unknown stop cause forbids a manual restart. |
| Starting | A committed claim owns the unresolved demand start. |
| Ready | A verified boot can accept a permit for its current claim. |
| Stopping | A saved stop intent blocks new claims. |
| Replacement-pending | A terminated instance needs the later replacement path. |

The journal records deployment, instance, Spot request, operation, boot, generation, and execution identities.
A claim records whether work is committed incoming work or active work.
The initial controller allows one claim.
A caller timeout retains an unresolved start claim.
The controller refuses claim release while a start or provider operation remains unresolved.
Idle stop and claim creation use the same lock.
A committed claim prevents idle stop.
A saved idle-stop intent refuses a later claim until stop confirmation.
A confirmed interruption takes priority over a pending idle stop.
The controller records `INTERRUPTED` and refuses a manual restart.

Phase C reserves compute before the next boot identity exists through `reserve_start()`.
`check_start()` rechecks current caps before bootstrap, start, and permit publication.
A cap refusal preserves the pending mutation and its token until the provider confirms the outcome.
The controller calls the read-only `HanaProvider.recover()` when spending refuses another mutation attempt.
Recovery continues after a cap refusal or reservation expiry.
`MutationUnresolved` means the original request can still complete.
Its stopped observation cannot close the compute exposure or replace the pending mutation.
`MutationSettled` confirms that the original request cannot produce further changes.
A settled stopped observation can close exposure and permit claim release.
An unknown stop cause still forbids a manual restart.
Recovery records exposure for a running box before it schedules a stop.
Recovery never starts the box or publishes a permit.
Each bootstrap, start, or publication attempt still needs accepted spending.
`bind_start()` checks spending after the provider verifies the boot identity.
A publication includes the expected prior generation and the current execution identity.
Command permission requires a current active claim, verified boot, acknowledged permit, and accepted spending.
Renewal retains the execution identity and advances the permit generation.
A refused or lost publication does not extend the guest deadline.
The controller reserves exposure until stop confirmation through the Phase C ledger.
It reconciles automatic restarts without a caller, including while placement is disabled.
An automatic restart cannot reuse an interrupted execution's boot challenge.

`HanaProvider.step()` defines the typed provider boundary.
A provider must bound its calls and use the saved operation tokens.
Its `recover()` method reads status without repeating a mutation.
It must distinguish a completed request from an unresolved request before reporting `MutationSettled`.
The provider refreshes bootstrap access before a demand start.
It must verify root receiver responses against deployment and demand-start identities.
It must install a renewed generation and permit together before acknowledging publication.
`hana_provider.AwsHanaProvider` supplies the concrete AWS provider.
The controller constructs it with the scaler profile and a sanitized credential environment.
An unprovisioned tick makes no provider call.
`Deployment.prepare_hana` and `Deployment.hana_setup` in `deploy.py` prepare and provision a separate Hana stack through the existing deployment helpers.
The user runs `prepare --workload hana`, then `hana-setup --approve-resources`.
Without approval, `hana-setup` prints only its local plan.
The existing preparation writer, preflight verifier, encrypted admin executor, stack waiter, and progress display serve both workloads.
The separate template is `docs/plans/builds-aws/hana-cloudformation.json`.
It creates a retained data volume, launch template, persistent Spot instance, security group, private control bucket, and scaler policy.
Its Spot interruption behavior is `stop`.
Setup uses Hana sizes and prices from `config/hana_aws.conf`.
It reserves setup costs within both account and workload limits before the stack request.
`reserve_setup()` permits only approved resource setup while placement remains disabled.
It does not permit demand starts or guest command admission.
Setup resumes an existing stack through reads before another mutation.
It refuses changed resource inputs or an expired reservation before another stack request.
It registers the deployment only after the Spot request and stopped state match.
Setup adds no route, target, or slot capacity.
Setup changes the stack through a change set, never through `cloudformation deploy`.
It refuses an Instance, Volume or VolumeAttachment replace or remove before it executes the change set.
A failed create or update calls `spot_end.end_boxes` before it reports the error.
That call keeps only the box that `hana/setup.json` records.
It then deletes, through `retained.delete`, the disks and buckets that the rollback kept (`DELETE_SKIPPED`).
A failed create also deletes the failed stack.
The launch template sets `DeleteOnTermination` true on the root disk, so AWS deletes each root disk with its box.

## Ending persistent Spot boxes

AWS opens a persistent Spot request again when its box is terminated.
The request then launches a new box with no tags.
`scripts/aws_target/spot_end.py` ends such boxes in this order:
1. Read the live boxes by deployment tag.
2. Cancel every live request of those boxes and every request with the tag.
3. Read the boxes again by tag and by request id, so a box launched between the read and the cancel is found.
4. Terminate the boxes and wait until they are terminated.
5. Read again. `SpotEndIncomplete` names any request or box that stays live.
A failed cancel raises before any terminate.
`deploy.py down --workload hana --approve-destroy` ends the Hana boxes, records the Hana spending end, and deletes the Hana stack.
It then deletes the data disk and the control bucket, and removes the local Hana records.
The data on those disks and in the bucket is lost; `--approve-destroy` is the approval.
`test_spot_end.py` uses a fake EC2 that relaunches a box when a box under an active persistent request is terminated.
It shows that a failed create and the Hana `down` each leave no request, box, disk, bucket or stack.
A failed update keeps only the recorded box and its root disk.
A template test shows that the Hana root disk has `DeleteOnTermination` true.
It also shows that each one-time launch uses one-time Spot with terminate.

The provider maps every remote operation through `PROVIDER_OPERATIONS`.
A policy test derives its required IAM actions from that mapping.
Start and stop actions require this deployment's Hana instance tags.
User-data changes also require `ec2:Attribute=userData`.
EC2 describe actions use the required wildcard resource.
Object reads and writes apply only to this deployment's control prefix.
No guest instance role or SSH key is created.
Every provider AWS request uses the configured `aws_timeout_s` bound.
Provider errors expose safe action names rather than cloud identifiers or signed URLs.

The provider persists its operation before dispatch and holds a separate mutation lock.
Recovery takes that lock without blocking an active mutation.
Recovery fences an operation that has not reached dispatch.
A late worker cannot dispatch that fenced operation.
A successful remote response remains saved when the controller response is lost.
Recovery verifies lost bootstrap writes through `DescribeInstanceAttribute`.
A lost start remains unresolved while the observed instance is stopped.
A pending, running, or stopping instance confirms that the start reached EC2.
Permit recovery remains unresolved until the permit deadline passes.
No recovery call starts the instance or publishes another permit.

The root receiver reads signed control objects and writes scoped acknowledgements.
Each demand start has new signed URLs and a separate control prefix.
The provider writes those URLs only into private runtime files and EC2 user data.
The root receiver verifies the deployment, demand start, current boot, prior generation, and permit deadline.
It acknowledges only an installed permit.
The guest shutdown fence rejects renewals.
The image must deny non-root IMDS access because user data contains runtime signed URLs.
The image must install the guard, watchdog, data mount, receiver, and runtime settings before setup.
The Natedev owner must provide that reviewed image through the separate `/etc/nixos` workflow.
The empty image and network settings prevent setup before that image review.

| New setting | Value | Reason |
| --- | --- | --- |
| `idle_stop_seconds` | `600` | Retain short bursts before a deliberate idle stop. |
| `start_timeout_seconds` | `120` | Bound caller waiting while retaining unresolved claims. |
| `tick_interval_s` | `5` | Reconcile before the independent boot guard expires. |
| `controller_tick_timeout_s` | `45` | Bound each supervisor child and failed provider step. |

The controller validates these intervals against the guest guard and permit windows.
Renewal starts with enough margin for two bounded steps and three tick intervals.
Tests use private journals, injected clocks, and a fake provider.
They verify that neither the controller lock nor the ledger lock remains held during provider work.
Regression tests cover expired bootstrap, start, and publication recovery.
They also cover stale recovery responses, concurrent cap refusals, and interruptions during idle stops.

## Removed builder: 2026-11-04

`builder-removal.json` records the removal on 2026-11-04.
The persistent `aws-build` instance, Tailscale device, and data volumes were removed.
`routing.json` records `enabled: false`.
The target registry contains `mac,natedev`.
The builder configuration and offload route have been removed.
Builder slots, warm mirrors, idle shutdown, and queue-based starts are historical features.
Only historical cost, ownership, and teardown compatibility remain.
The foundation template has no builder instance, launch template, data volume, roles or stop action.
The `remove-builder` command and the `down` compute stage are removed.
The foundation template has no builder parameters or outputs, such as `EnableCompute` or `ComputeEnabled`, since 2026-11-15.
`deploy.py` has no legacy record migration, data volume adoption, saved builder image or compute request reconcile.
`down` reads a record without `ComputeEnabled` as a foundation with no builder.
The ledger still prices the recorded builder hours with `instance_type` and `spot_max_usd_per_hour`.

On 2026-11-04 an `up` with `cloudformation deploy` replaced the builder instance, and the update failed.
The rollback terminated the new instance, so its persistent Spot request opened again.
AWS then launched a stopped box with no tags, which stayed until the user removed it.
Since that day, `up` uses a change set and refuses an instance or volume replace before it executes.

`deploy.py down --approve-destroy` can remove the remaining gate resources without registering the former target.
Teardown resolves former builder claim and lock paths directly from the AWS state folder.
It preserves the existing lock and live claim checks.
The builder controller and remote agent have been removed.
Teardown still checks retained claims before deletion.

## Paired speed trials

Run `python3 -m scripts.aws_target.aws_trial --base <ref> --changed <ref> --modules <folder>/<file.py> --rounds 3`.
Both refs can name local commits in `~/.claude`.
The trial pushes each commit to a fresh `gate-trial-<expires>-<id>-base|changed` branch with a plain push.
The expires field records the trial's fixed expiry epoch in seconds.
The trial folder's UUID supplies the id; `trial_branch_prefix` configures the prefix.
At trial end, cleanup lists the trial's remote branch namespace and deletes each branch, including after errors and Ctrl-C.
The trial installs no signal handler.
Ctrl-C and terminal command exceptions stop the paired service.
SIGTERM or SIGHUP to the terminal can skip branch cleanup.
The separate trial service then runs to its normal end and cleans its boxes.
The paired unit uses `ExecStopPost` with `aws_trial --paired-cleanup <folder>`.
After a service stop, systemd runs this cleanup after killing the coordinator and its children.
Cleanup requests termination for all recorded boxes before searching uncertain launches.
Cleanup requests termination for each discovered box before the next search.
Cleanup checks final states after these requests.
Cleanup uses one deadline from `termination_timeout_s` for all boxes.
Each API call uses the time that remains before this deadline.
The service stop timeout adds twice `api_timeout_s` to this deadline duration.
Cleanup records confirmed termination in the cost ledger.
Confirmed termination is final for every later ledger outcome.
Later running, absent, or terminated updates cannot change its end time or cost.
A worker rejects an ended booking before checking for a launch file.
The ledger rejects reservations for closed attempt IDs.
A retry uses a new attempt ID.
Cleanup resolves reservations without saved launch arguments as `GateAbsent`.
The module and coordinator also call this cleanup from `finally` blocks.
Repeated cleanup does not change resolved reservations.
AWS failures leave reservations unresolved for later recovery.
Before reservation, the coordinator cleans expired earlier trials with unresolved reservations.
The sweep checks each attempt's owner and passes only due attempts to cleanup.
The sweep preserves boxes with live owners, even when another owner in the same expired trial has ended.
The sweep preserves trial folders.
Guest expiry and the AWS expiry function remain independent termination controls.
AWS failures can delay termination beyond the configured expiry and confirmation timeout.
Merge gate units also use `ExecStopPost` with `cloud_gate --cleanup`.
Trials and merge gates use the same attempt cleanup code in `cloud_gate.py`.
The worker installs no signal handler.
The ledger records each worker's PID, process start identity, and attempt folder.
Reconciliation preserves live workers and workers whose identity cannot be read.
Reconciliation terminates expired attempts and attempts whose recorded worker has ended.
Each worker reconciles before reusing a saved launch.
A hard kill without a later start can leave the booking open.
The ledger continues to estimate its cost for the spending cap.
The AWS expiry function ends the box.
The next gate or trial start closes the booking after confirmation.
Before new pushes, a start sweep deletes parsed trial branches whose expiry is earlier than the current time.
The sweep reads no current lifetime setting.
A lower lifetime setting leaves existing trials' fixed expiries unchanged.
The sweep preserves unexpired branches, malformed names, and unrelated branches.
A sweep failure prints one stderr line and the trial continues.
This cleanup covers a push accepted remotely before the client reports an error.
Each network Git call uses `git_network_timeout_s`, which defaults to 60 seconds.
A timeout or cancellation kills the Git process group, including SSH children.
A cleanup failure prints one stderr line for the trial or branch and preserves the trial's exit code.
The pushes use the invoking terminal's Git credentials.
Each module uses one disposable gate box.
All boxes launch after the full trial obtains its cloud slots and cost reservations.
The trial refuses costs above the daily or monthly room.
It does not change either limit.
It uses the scaler service, the existing launch template, and presigned transfers.
It reads no administrator secret.

Each box checks both trees and runs a separate `module_run.py` process for every sample.
Each box first runs base and changed once in separate processes without measurements.
These warm-up runs use the same process isolation as measured runs.
The result rows and medians exclude the warm-up runs.
The default remains three measured rounds.
The reserved box lifetime includes both warm-up runs and all measured rounds.
The guest divides the available time across warm-up runs and measured rounds.
Odd rounds run base first; even rounds run changed first.
The changed candidate supplies the supervisor, environment, and memory sampler.
Each tested tree supplies `module_run.py` and the test module.
Periodic log transfers stop before measurement.
The guest transfers each sample between measurements.
Each paired run writes a UTC `stage=` line through the shared stage writer.
Warm-up stages are `warm-up base` and `warm-up changed`.
Measured stages are `round <r> of <N> <variant>`.
The guest writes `finish` after the last run.
It publishes the current stage before each run through the existing per-run log transfer.
The final transfer includes `finish` and the last result rows.
These transfers include `boot.log`, `gate.log`, and `timeline.log`.
The upload count and measured result fields remain unchanged.

`cloud_gate.log_cloud_progress` reads stages for both paired trials and merge gates.
The progress record adds `stage`, `region`, and `zone` beside the existing fields.
Before the first box log line, the state is `requesting` and the stage is `launch`.
The last UTC `stage=` line supplies the stage text.
A `round` stage sets the state to `running`.
Once the merge runner writes `Runner:`, the stage text becomes empty and the existing module counter applies.
`Cloud gate: uploading` sets the stage text to `uploading`.
Each log download publishes the progress beside the reservation sidecar in `.cloud.json`.
After launch, every progress publication includes the launched region and availability zone from the saved variant.
Before launch, the region and zone are empty.
Older records default all three new fields to empty strings.
Older readers ignore these extra fields.

Stdout contains run JSON lines, module JSON lines, and one final cost JSON line.
The module summary gives the median CPU saving across complete pairs.
A failed or missing sample makes `all_passed` false.
The final line contains `total_cost_usd`, `cost_basis: "estimate"`, and `cleanup_complete`.
The estimate counts only this trial's boxes, allowances, and new bill-query fees.
Unconfirmed termination uses the reserved lifetime and returns a nonzero status.
The command terminates boxes after results, errors, or cancellation.
Uncertain launches retain their requests and reservations for reconciliation and expiry cleanup.

The paired trial replaces the size/start trials.
The old size/start trial settings are removed.
The paired trial uses the existing `cloud_gate.conf` settings.

## Cloud gate region fallback

The deployed region list comes from `regions` in `cloud_gate.conf`.
The list must start with the home region and must not repeat a region.
The list holds every region of `spot_candidate_regions`, so a region that becomes the cheapest gets the next gate.
The user, 2026-11-15: "i want all regions availble so if one becomes cheaper then it will immediatelyh start being used".
`us-east-2` remains the home region for shared artifacts.
The list can include other regions that the account policy permits.
The gate ranks deployed types and public zones by the expected USD per run before launch.
Equal costs retain the configured region, type, and zone order.
Capacity refusals and unsupported types select the next variant.
A normal gate uses natedev after all capacity choices refuse.
A confirmed Spot interruption before a final result starts another cloud attempt.
`spot_interruption_retries=2` permits two retries, which can reach another zone and then another region.
The retry uses the next choice in the saved launch order.
The route keeps its cloud slot across retries.
The route saves the interruption count and the next choice in `current.json` before each retry.
Resume restores both values, so a crash cannot reset the retry budget.
Each retry gets a new attempt ID and passes the existing spend and attempt checks.
The worker confirms cleanup of the interrupted box and reservation before the retry.
The final natedev fallback row includes the interruption count when retries or capacity choices end.
A complete gate failure keeps its failed result and gets no retry.
An unconfirmed termination or another controller error retains the existing fallback behavior.
The launch path uses Spot only; it has no on-demand choice.
Paired trials use the same regional launch and cleanup path.

The home stack owns the artifact bucket and the global gate role and instance profile.
Each fallback region has a separate gate stack in its default VPC.
The deployment generates each regional template from the home template.
The regional template contains network, launch, scaler policy, and expiry resources.
The regional scaler policy keeps the home tag, type, Spot, and launch-template restrictions.
The regional expiry function terminates expired boxes in that region.

The deployment resolves the pinned image by Name and owner or copies it when no matching image exists.
It records copies before it waits for image readiness.
The deployment record stores ordered regional launch resources and keeps artifact resources at the top level.
The reader migrates an old single-region record once.
Launch state, launch logs, and reservations retain the box region.
Recovery replays saved launch arguments in the saved region before it uses the current variants.
Status, termination, and expired-launch searches use that region.
Reconciliation searches every configured region for an expired reservation without a recorded region.
After a valid final result, the controller books the box end at the result receipt time.
It still requests termination and requires AWS to confirm termination before it books that time.
A box without a valid final result uses the termination confirmation time.
Reconciliation and the expiry sweep retain their existing end times.
Results include `kernel_boot_at` from `/proc/stat` and `user_data_started_at` from the first user data command.
Each timestamp is a UTC epoch time from the box.
Results include `instance_running_at` when a cached instance identity document supplies `pendingTime`.
The live `gate_box.GUEST` program contains the bootstrap code.
The bootstrap downloads or clones the candidate and verifies its requested tree.
It prepares the environment and calls the candidate's `scripts.aws_target.gate_box.run_guest` entry point.
The entry point accepts the box directory, links, and mutable result dictionary.
The candidate supplies its own suite, timing, and record code.
The guest installs no signal handler.
The module time limit and process groups stop the gate.
The bootstrap names no `merge_gate` attribute and copies no host function source.
A paired trial uses the changed candidate's entry point to supervise both verified trees.
A candidate without the entry point publishes `candidate predates guest entry point` through the existing boot error link.
The controller then uses the normal natedev fallback.
The result fields and transfer links keep their existing format.

The guest reads the NixOS metadata cache or the cloud-init metadata cache without network requests.
It omits `instance_running_at` when the cache is absent or invalid.
The current source checkout occurs after `nix_ready_at`, so results omit `source_ready_at`.
All regions use one spend ledger and the existing caps.
Gate costs use bill rates for the recorded region and UTC day.
A day with its own completed bill keeps that day's rate when newer bills arrive.
An unbilled day uses the newest earlier bill until its own bill arrives.
A day before every bill uses the oldest available bill.
Reservations store the selected rates and bill days for each UTC day.
Each pricing pass builds one bill index for each type and region.
Reservations that start after the current time retain their full reserved compute cost.
Intervals that cross UTC midnight use each day's rate for that part of the lifetime.
The minimum charge applies once per lifetime.
The older builder and paired trials use the same daily rate rule.
Reservations without a region use the highest configured regional rate.
A region without a usable bill rate uses the configured rate ceiling.
Completed gates also use bill rates for non-compute costs.
Public IPv4 and gp3 root storage use regional rates over the gate's actual box hours.
The root charge uses `root_gib` and the number of hours in the bill's month.
S3 requests, storage, and outbound transfer use the bill day's totals divided by its gate launch count.
The ledger charges the S3 share once per gate.
The hourly charges use each UTC day's rate across midnight.
These rates use the same bill day rule as compute rates.
The ledger stores the selected cost and its bill source.
Paired trials use this booked cost for completed gates.
Open reservations retain `attempt_allowance_usd`.
Admission includes these allowances when bills exist.
An own-day bill corrects saved estimates that still include configured allowances.
The bill remains the minimum cost for its reconciliation period.
Missing cost rows contribute zero.
The configured allowance supplies the cost when no needed bill rows exist or the S3 share has no launch count.
The spending limits do not change.

European deployment needs account and policy changes before the regional stacks can start.
Enable `eu-south-1` and `eu-south-2` on the AWS Account page and wait until each status is `ENABLED`.
See [AWS Region activation](https://docs.aws.amazon.com/accounts/latest/reference/manage-acct-regions.html).
Allow every region in `regions` and `spot_candidate_regions` in the `RegionFloor` statement of the Organizations policy `AdvancedModeRegionRestrictionSecurityControlPolicy` before deployment; the runbook names all 19.
No other statement in that policy may deny EC2 in these regions.
Keep the existing home and US region permissions.
The runbook gives the preparation steps.

`buildlog.timer` runs the hourly Spot snapshot through `buildlog hourly` on natedev.
The existing timer uses `OnUnitInactiveSec=3600s` and calls the installed scripts.
The hourly job calls `aws_target.hourly`: the Spot snapshot, then the billed hours check.
`python3 scripts/aws_target/aws_target.py snapshot` runs the same two steps manually.
No systemd unit change or NixOS rebuild is required.
The old `aws-target.service` remains disabled.
`aws_target.py tick` reads account charges only.
`price_refresh_seconds` limits snapshot attempts to one per configured interval.
`deploy.py up` runs the same reader with `force=True` after the stack update, so prices fill at once.
A forced read does not move the interval, so the next hourly read still runs.
The hourly timer starts one hour after the last run ends, so two hourly reads are always more than 3600 s apart.
A price read is therefore never older than one hour plus the run time.
It tries `deploy_price_fill_attempts=3` times, `deploy_price_fill_pause_s=10` seconds apart, while IAM shows the new policy.
The deploy prints the reasons and sends no alert; a failed fill does not fail the deploy, and the hourly refresh sends the alert.
The snapshot takes `spot-prices.lock` without blocking.
It reads each distinct `spot_candidate_regions` region once.
Each regional read requests all `instance_types` and `gpu_instance_types` prices for Linux/UNIX.
The AWS CLI reads all pages, and the reader selects the latest price for each type and zone.
The reader also reads applied Standard Spot and G and VT Spot quotas.
It uses the scaler `credentials_profile` and the private `credential_file` without an administrator passphrase.
It removes inherited administrator keys and profiles from the AWS process environment.
The only API actions are `ec2:DescribeSpotPriceHistory` and `servicequotas:GetServiceQuota`.
The CLI service name is `service-quotas`; the IAM prefix is `servicequotas`.
See the [AWS authorization reference](https://docs.aws.amazon.com/service-authorization/latest/reference/list_service-quotas.html).
The snapshot test derives IAM actions from every request in the real snapshot path.
It checks the scaler policy for each action, all resources, and no regional condition.
A missing deployed policy requires `deploy.py up --workload merge-gates --approve-resources`.
The table and snapshot commands never deploy a policy.
The snapshot atomically writes prices and quota values together in `spot-prices.json`.
The existing `prices` array keeps its ranking format: `region`, `zone`, `instance_type`, `usd_per_hour`, and `read_at`.
The new `regions` array stores `region`, `read_at`, `standard_vcpus`, and `gpu_vcpus`.
After that write, the snapshot adds each read to `spot-price-days.json`.
For each type and region, it adds the lowest zone price of the read to that UTC day.
Each `days` row keeps `instance_type`, `region`, `day`, `usd_sum`, and `reads`; the mean is `usd_sum / reads`.
The file keeps the last `spot_price_days_keep` days from `config/aws_target.conf`.
A run estimate can then use the Spot price of each billed day.
A failed history write leaves `spot-prices.json` written and returns a failure status.
A regional failure preserves that region's previous prices, quotas, and read time.
Other regions still refresh, and the command returns a failure status.
The `failures` array saves the latest failed attempt for each region.
Each record contains `region`, `at`, `read`, `error`, and the bounded, redacted `reason`.
A successful regional read removes that region's failure record.
Old snapshots without failure records remain readable.
The `failure_streak` record stores `since` and `notice_claimed` while any candidate region fails.
The reader also accepts the previous `notified` field.
The snapshot saves each failure before it attempts a user notice.
It renders the registered `aws.snapshot_failed` text and sends it through `push_texts.user_send_argv`.
The message key comes from the streak's start time.
The notice names each region and read that AWS refused. The saved failure keeps the AWS error code.
`AccessDenied`, `AccessDeniedException`, `UnauthorizedOperation`, and `not authorized` permit a notice.
The fixed action requests `deploy.py up --workload merge-gates --approve-resources` on natedev.
That command updates permissions but does not replace scaler credentials.
Credential errors, timeouts, network errors, and throttling send no phone notice.
`deploy.py apply --approve-resources` replaces the scaler key and checks its identity.
The table continues to show each regional failure.
A failure without a notice preserves the streak without claiming a notice.
A later permission error in that streak can send one notice.
The registry requires user approval for the new text.
The approval file remains unchanged.
The snapshot saves the notice claim before it calls the message process.
The claim prevents another send for that streak after a crash.
A crash before delivery can suppress that streak's notice.
A normal delivery failure clears the claim for the next snapshot attempt with the same key.
Success in every candidate region ends the streak and drops any unsent notice because no user action remains.
A later failure starts a new streak and permits a new notice.
`snapshot_notification_timeout_s=120` bounds message review and delivery in seconds.
Each failure logs the region, the failed read, the error class, and a redacted reason on one line.
The read name is `price`, `Standard quota`, or `G and VT quota`.
The shared `admin_secrets.redact` helper removes AWS account ids, ARNs, access key ids, and instance, subnet, AMI, volume, and snapshot ids.
The helper first removes credential field values and all following text, including wrapped values.
It preserves credential fields that already contain `[redacted]`, so repeated redaction keeps appended deployment reports.
It then removes known credentials and AWS identifiers, including wrapped account ids, access key ids, and ARNs.
The snapshot first removes request dumps, response dumps, headers, structured dump lines, and all lines after each dump marker.
It redacts the remaining raw text and selects the first AWS or CLI error line that matches `SNAPSHOT_ERROR_PATTERNS`.
Every pattern matches only at the start of a line.
The patterns accept both CLI error formats, `aws: error:` and `aws: [ERROR]:`, with any letter case for `error`.
They also accept standalone `Invalid choice` and `Parameter validation failed` lines.
If no line matches, it selects the first non-empty line.
It discards all other lines, including usage text and request dumps.
It normalizes that line's whitespace and redacts it again.
`snapshot_error_reason_max_chars=240` limits the reason after both redaction passes.
A successful regional read removes previous prices for types or zones absent from the new reply.
The snapshot does not change `controller.json`.

`spot_table.spot_tables` reads only the local snapshot, cost ledger and configuration.
It is the one source of the price tables of the AWS page; no terminal output remains.
The user, 2026-11-15, removed the `/aws_prices` command and its `deploy.py spot-table` output because the page shows the prices.
It gives two price tables: the standard types, then the GPU types.
Each has candidate regions as rows and machine types as columns.
The user asked for this layout on 2026-11-15 through builds, because the regions outnumber the types.
Each cell shows the lowest zone price in USD/h.
A type that a region does not sell shows `—` (`spot_table.NOT_SOLD`), in `Table.not_sold`.
Another empty cell says `no snapshot` or `read failed`.
Cached prices and quotas remain visible after a read failure.
Their price cells include `read failed`, and the regional detail gives the last good age.
Old prices include `old <minutes> min` in their cells.
Each failed region shows the saved error time, age, failed read, error class, and AWS error line.
Each region without evidence says `no snapshot was ever taken`.
The table never uses a bare `-` for an unavailable value.
Each region shows its snapshot age and a stale mark after `price_max_age_seconds`, under the prices.
Below the prices, two quota tables show the Standard quota and the G and VT quota in vCPUs.
Each also shows the number of boxes of its types that fit: standard types in the first, GPU types in the second.
One region read writes the prices and both quotas together, so the quota cells show no age.
The box counts divide the quota by the existing type-size vCPU calculation and round downward.
An absent snapshot explains missing quotas and box counts without creating files.
The standard rows are `instance_types`, then `price_compare_instance_types`; the GPU rows are `gpu_instance_types`.
The hourly snapshot reads prices for all three lists, so the page can compare types the gates do not use yet.
Below the quotas, the page shows one run table for each standard type, with one line for each candidate region.
The user asked for this layout on 2026-11-14 through builds. Each line shows:
1. The lowest zone Spot price in USD/h.
2. The median merge gate box time in minutes with one decimal, such as `7.5`, from launch to finish, boot included.
   Times come from `cloud-gate-launches.jsonl` over the last `run_cost_days` days.
3. Runs: the run count of that time.
   A region without its own runs uses the type's runs in other regions, and the cell says `elsewhere`.
   A type without runs uses the runs of the types with its vCPU count, and the cell says `same vCPUs`.
4. The estimate: Spot price times that box time, plus the refusal wait cost.
5. Paid USD/h: the bill's `SpotUsage:<type>` cost for the type and region, over its billed hours.
6. Paid USD/run: that bill split between the runs of both kinds by box time, for the merge gates only.
   The cell also counts the runs whose day has no bill yet, such as `0.0314 (17 bill due)`.
   A bill comes up to about a day late.
   A region whose runs all wait for a bill shows `17 bill due`; a region with no own runs of the type shows `no runs`.
7. Paid runs: the count of runs that paid USD/run covers.
The bill names the region by its usage type prefix, such as `USE2-SpotUsage:c7a.8xlarge`.
`ledger.REGION_USAGE_PREFIXES` maps each prefix to a region.
A launch row without a region ran in the home region, `region` in `aws_target.conf`.
The next line names the cheapest merge gate by estimate.
Below them, two tables show the refused requests and the launched boxes of each type and region.
Refused requests are capacity and quota refusals.
The last table shows billed hours / box hours for each type and region, from `run_cost.hours_checks`.
A cell is `<billed>/<box> (<gap>)`, such as `9.2/8.8 (4%)`, with `over` when `hours_gap_over` is set.
A type and region without a bill read shows `not billed`. Only types with hours have a row.
An unreadable cost ledger shows a note in place of the billed hours.

The AWS page shows the same tables, published by `aws_page.publish`.
The user renamed it from AWS prices to AWS on 2026-11-15, because it holds spend, prices and quotas.
The published file stays `aws_prices.html`, so older links still open it.
A Spend card at the top shows today, yesterday and the month to date, with the month forecast below them.
`spend.read_spend` and `spend.month_spend` give these numbers; the buildlog report reads the same functions, so both show the same values. Read the spend on the console AWS page; the `/aws_spend` command was removed on 2026-11-16.
The day is the UTC day. The hourly buildlog run publishes the page, so the spend is current to the last hour.
After each push to main, `.claude/config/post_push.sh` publishes it again
when the push changes the page code and the pushing checkout is at the pushed commit, as the promotion step's ~/.claude is.
The Prices and Quotas sections follow the Spend card.
The user asked for these page changes on 2026-11-14 through builds:
1. A switch above the two price cards shows `Spot price` or `Cost per gate`; the user asked for it on 2026-11-15 through builds.
   `Cost per gate` shows a second switch, `Merge gates`, `Targeted runs` or `Both`, and it sets both cards.
   The cost cells are the estimate in USD for one run (`RunCosts.prices`), the estimate that gate placement uses.
   A type and region with no estimate shows a dash. GPU types have no runs, so their cost cells are dashes.
   The run kind switch also sets the run tables below. The URL keeps both choices, as `?prices=cost&runs=gate`.
2. Each column has a three-color scale: green at the minimum, white at the median and red at the maximum.
   In dark mode the middle is the card color. A ban cell, a dash and a column with fewer than two values have no color.
   A white box marks the lowest and the highest cell of each type column, in both themes; the user asked for it on 2026-11-15 through builds.
   The box is 1 px white inside a 1 px dark edge, which keeps it visible on the light page. Equal cells all have the box.
   A column with only two values has no box, because the colors already show which is which.
   The cheapest cell of each table is bold on a gold background (`--cheapest`), with dark text and a 2 px white outline (`--mark-edge`).
   The most expensive cell of each table is bold on a black background (`--dearest`) with white text and the same white outline.
   Both are the same in both themes, and they replace the scale color and the box of that cell.
   The user asked for them on 2026-11-15 through builds, because bold alone did not make the cheapest cell stand out.
   The user asked for the white outline on the gold cell too, so both table marks match.
   A legend line above the cards explains the colors.
   A legend line under each price card that has a mark names the box, the gold cell and the black cell.
   Builds removed the vCPU group mark on 2026-11-15.
3. Each standard and GPU table is its own card. The two cards sit side by side and stack below 760 px.
   The content fills the window width, less a 16 px gutter on each side; the user asked for it on 2026-11-15 through builds.
   The region chips have 6 px padding and a 4 px gap, so the 21 chips fit one row in a 1920 px window.
   The user asked for the side by side cards on 2026-11-15 through builds.
4. A header of the form `name (unit)` shows the unit on a smaller second line.
5. The run kind filter also selects the refused and launched cards. The billed hours card does not change with it.
   The page run tables have no Spot or estimate column, because the cost view shows those numbers.
6. A `Table.not_sold` cell shows the lucide `ban` icon, with the title `Not sold here` on hover, and no legend.
   The user asked for it on 2026-11-15 through builds.

A region filter at the top of the page shows all regions by default; the user asked for it on 2026-11-14 through builds.
1. A chip for each region toggles it. `All` shows every region again.
   The chips show the groups `us`, `eu`, `ap`, `ca` and `sa` in that order, each A to Z; a region outside them comes last.
   `aws_page.region_filter_key` sets the order; the user asked for it on 2026-11-15 through sdlc and builds.
   `None` at the end clears every region, so one click then shows only that region; the user asked for it on 2026-11-15 through builds.
   With no region chosen, each card says `No region chosen.` A reload with no region saved shows every region.
2. Every section follows it: tables with a region column drop the other columns, and tables with a region row drop the other rows.
3. The colors, the box and the table marks use only the chosen regions. `Table.values` holds the number of each cell.
   One region shows no color and no box.
4. The cheapest line names the cheapest estimate in the chosen regions (`RunCosts.estimates`, cheapest first).
5. A snapshot line for one region shows only when that region is chosen.
6. The page keeps the region choice in browser storage. The URL keeps the price view and the run kind.

### Billed hours check

The user asked for this check on 2026-11-14 through builds.
It compares the billed Spot hours with the recorded box hours for each type, region and UTC day.
1. Billed hours: the `UsageQuantity` of `<CODE>-SpotUsage:<type>` in the bill read for that day.
   A day with a bill read and no usage row for the type bills 0 hours.
2. Box hours: each gate in the cost ledger, from its reservation to its confirmed end, split at UTC midnight.
   Open and absent gates do not count. A gate without a region ran in the home region.
   The builder box also counts, as the `aws_target.conf` `instance_type` in the home region.
   Its time runs from each `running` observation in the cost ledger to the next other state.
   A `running` observation with no later state counts to the window end.
   Reason: on 2026-11-08 the builder box ran 8.8 h, and the check reported its Spot hours as a gate gap.
3. The gap ratio is `abs(billed - box) / box`.
   A gap above `billed_hours_gap_max_ratio=0.15` in `aws_target.conf` is over.
   Billed hours with no box hours are also over.
`run_cost.day_hours_checks` gives one `HoursCheck` per type, region and day in the window.
Days run from the window start day up to, but not including, the end day, like the bill split.
`run_cost.hours_checks` sums the days with a bill read for each type and region, for the billed hours table on the AWS page.
`HoursCheck` holds `billed_hours`, `box_hours`, `hours_gap_ratio` and `hours_gap_over`.
Without a bill read, `billed_hours` and `hours_gap_ratio` are `None`, and `box_hours` counts every day.
`aws_target.hourly` checks the last `run_cost_days` days after its price read.
`buildlog hourly` and `aws_target.py snapshot` both call it.
It checks the types the snapshot reads: `instance_types`, `price_compare_instance_types` and `gpu_instance_types`.
Each type, region and day that is over sends one `aws.hours_gap` user notice.
The key `aws-hours-gap:<type>:<region>:<day>` is saved in `hours-gap-notices.json` before delivery.
A failed delivery removes the claim, so the next hourly run sends it again.
A claim for a day before the window is dropped.
Live, 2026-11-14: the days since 2026-11-12 are within 4.5 %.
Before that day, the box hours were 7 to 13 % above the bill.

`python3 scripts/aws_target/aws_target.py open-gates` reads only the local cost ledger.
It prints the ledger path, file write time, age, counts by state, and pending compute request count.
It lists each open attempt, state, reservation time, expiry, instance type, region, and mode.
It never prints an instance id.
`ledger.gate_is_open` defines open entries as `GateLaunchPending` or `GateRunning`.
The spending report uses the same function for open reservations.
Expired reservations remain open until AWS confirms their closure.
The command calls `CostLedger.read_snapshot()` and `pending_compute_requests()` without locks, writes, or AWS calls.
It returns 1 when a gate reservation is open and 0 when none is open.
Missing or unreadable evidence returns 2.

A new gate ranks only variants from its deployment.
`deploy.py up` saves the deployed `instance_types` in `gate-deployment.json`.
A gate skips a configured type that this list does not name, and prints `Run deploy.py up`.
When the list names no configured type, the gate falls back to natedev before any launch.
A manifest without the list keeps every configured type.
An `UnauthorizedOperation` refusal skips that type for the rest of the launch while another type remains.
`launch.json` saves the skipped types in `unauthorized_types`.
Before its first ranking, it reads live Spot prices in parallel from each deployed region.
Each region receives one Linux/UNIX request for all configured `instance_types`.
`spot_price_live_read_timeout_s=3` bounds the whole parallel read in seconds.
The reader selects the latest price for each deployed zone and type.
Live rows replace matching hourly rows for this ranking only.
Failed or slow regions retain their hourly rows.
The live read never writes `spot-prices.json`.
`launch.json` saves `spot_price_reads`, with `region`, `seconds`, and `used` for each regional call.
Launch and refusal rows copy this evidence into `cloud-gate-launches.jsonl`.
Resumed launches keep the saved ranking and evidence without another live read.
Placement does not try a type and region whose median box time, boot included, is above `max_gate_minutes=12`.
The user, 2026-11-14: a slow type costs more gate time than its lower price saves.
The median uses the type's runs in that region, or its runs in all regions when the region has none.
A type without runs is tried; `lifetime_minutes` is not a measurement.
Placement also does not try a type in a region whose Spot quota in the hourly snapshot is below one box of that type.
The showrunner, 2026-11-14: a region with a pending quota request must not get launch attempts.
A G or VT type uses the G and VT quota; other types use the Standard quota.
A quota that is missing or older than `price_max_age_seconds` does not cause a skip.
Each skip prints `Cloud gate skips <type> in <region>`.
When every variant is skipped, the gate falls back to natedev before a launch.
Available variants precede variants under a capacity cooldown.
Within each group, variants with a current price come first, ranked by the expected USD per run.
The user, 2026-11-14: "base our spot Requests on lowest price per run".
Equal costs follow the `instance_types` order.
Without any current price, the configured region, type, and zone order stays.
That cost includes the hourly Spot price times the expected box hours and the remote region transfer charge.
It also includes regional extra seconds times `gate_wait_usd_per_minute`, divided by 60.
`region_extra_gate_seconds={}` holds a JSON map from region names to measured extra seconds.
Missing regions contribute zero extra seconds.
`unmeasured_region_extra_seconds` holds a JSON map from region names to seconds.
A region without its own runs borrows box time from the other regions and adds its seconds from this map.
The seeds come from the 2026-11-14 runs: Europe 15 s, Canada 10 s, South America and Asia Pacific 30 s.
After the first run in a region, its own median box time replaces the borrowed time and the extra.
`gate_wait_usd_per_minute=0` disables the wait cost until the delay trial and builds review set it.
It also includes the expected refusals: refused requests per launch for the type and region, times the median wait of one refusal, times `gate_wait_usd_per_minute`.
Refusals count the zone and quota codes below over the last `run_cost_days` days; `UnauthorizedOperation` does not count.
Expected box time is the median box time, launch to finish, for the type, region and mode over the last `run_cost_days` days.
A region without its own runs uses the type's runs in all regions.
A type without runs uses the runs of the types with its vCPU count, so m7a.8xlarge uses the c7a.8xlarge runs.
The user, 2026-11-16: "it should be entirley based on who has the lowest price per run".
Without runs of that vCPU count, it uses all runs of the mode.
Without any runs, it uses `lifetime_minutes`.
The AWS page shows the same expected cost and refusals per launch, without the remote transfer and regional delay.
Its expected cost includes the unmeasured seconds.
`remote_region_transfer_usd=0.00015` applies when the region differs from the home region.
Local measurements cover 692 gates with a source bundle, request, and result.
Their median sizes are 10,157, 32,926, and 7,051,583 bytes, respectively.
The median combined size is 7,107,672 bytes.
At $0.02 per decimal GB, this costs $0.00014215344; the configured value rounds upward.
`region_transfer_usd` holds a JSON map from far regions to their transfer USD per gate.
Each value is about 10 MB per gate, the 2026-11-14 bill, at that region's inter-region transfer price.
A region outside the map uses `remote_region_transfer_usd`.
Equal costs keep configured order.
Missing or stale prices follow fresh prices.
Without applicable fresh prices, the gate preserves configured order except for capacity cooldowns.

`capacity_cooldown_seconds=1800` moves recently refused capacity to the end without removing it.
`InsufficientInstanceCapacity`, `InsufficientSpotCapacity`, and `SpotMaxPriceTooLow` affect one region, type, and zone.
`MaxSpotInstanceCountExceeded` and `VcpuLimitExceeded` affect every variant of the region.
Ranking reads the end of `cloud-gate-launches.jsonl` in 64 KiB blocks.
It stops at the first row before the longer of the price-age and cooldown windows.
The current two-hour window reads 65,536 bytes from a 750,584-byte local history file.
`launch.json` saves the ranked region, type, subnet, and zone list before the first launch step.
Resumed launches keep that list and their offset-based ClientToken values.
Older files with only `variant` retain configured order.
Refusal handling, regional cost bookings, and the $35 daily and $400 monthly limits remain unchanged.

The prices command prints regional Spot prices without resource identities.
It reads gate regions, then `spot_candidate_regions`, without duplicates.
It preserves the configured order within each list.
`spot_candidate_regions` supplies the candidate regions for Spot work and price reads.
`gpu_instance_types=g6.xlarge g4dn.xlarge g4ad.xlarge g5.xlarge` adds GPU types to the existing price rows.
The table header follows the administrator passphrase prompt.
Whole quota values have no decimal suffix.
`deploy.py prices` shows the Standard Spot vCPU quota (`L-34B43A08`) for every region in its combined list.
`deploy.py prices` also shows the G and VT Spot vCPU quota (`L-3819A6DF`) for each region in its list.
The reads use the encrypted administrator key through the admin executor.
A region whose quota is below the largest configured type prints a request for a quota increase.
The vCPU count comes from the type size: `large` is 2, `xlarge` is 4, and `<N>xlarge` is 4 × N.
A type with another size has no known count, and the check skips it.
A failed read prints the quota as unknown with the redacted reason, and the command continues.
The administrator IAM policy is outside the repository and needs `servicequotas:GetServiceQuota`.
`deploy.py quota-request` requests Standard Spot quota (`L-34B43A08`) in each gate region from `regions`.
Credential refusals print their message once and stop the command with a nonzero exit status.
AWS failures still print the AWS error code and allow the next region's request.
It uses `spot_quota_request_vcpus` from `cloud_gate.conf`.
The default request is 136 vCPUs for four 32-vCPU gates and one 8-vCPU Hana box.
The same command requests G and VT Spot quota (`L-3819A6DF`) in each gate region from `regions`.
A region in `spot_candidate_regions` only has no gate stack and gets no quota request.
`deploy.py quota-status` reads the same gate regions.
It uses `gpu_spot_quota_request_vcpus=32` for one GPU box up to 8xlarge.
All three new keys are in `cloud_gate.conf`.
Both quota codes use one helper for regional requests.
The command uses the encrypted administrator key through the admin executor.
It skips regions with sufficient quota and prints existing open requests instead of creating duplicates.
It prints the region, desired value, and request status without request, case, or account identifiers.
A failed call prints only the AWS error code or Python error class and does not stop other regions.
Any regional failure makes the command exit with status 1.
The external administrator IAM policy also needs `servicequotas:ListRequestedServiceQuotaChangeHistoryByQuota` and `servicequotas:RequestServiceQuotaIncrease`.
Both quota codes use these same three IAM actions.
`python3 scripts/aws_target/deploy.py quota-status` reads both quotas and their request history through the same administrator executor.
It shares the quota codes and region lists with `quota-request`.
Each region starts one terminal block with its name as the heading.
Each quota shows its applied vCPUs once, above an aligned request table.
The table shows asked vCPUs, status, case ID, and update time, with the newest requests first.
Dates use the user's zone and the last update time, with the creation time as a fallback.
A quota with no requests prints `No requests.`
The final summary lists regions whose applied GPU quota fits the trial and requests that remain open.
The summary uses the trial's shared vCPU calculation over `gpu_size_trial_spot_max_usd_per_hour`.
It does not use the requested quota as proof of an applied quota.
When every applied quota is too small, the summary says `no region has enough applied quota`.
An unread applied GPU quota adds `unknown for <regions>` after the regions that fit.
An unread request history makes the summary say `open requests: unknown` and name the unavailable region and quota.
The report omits account IDs, ARNs, and request IDs.
Regional failures print the AWS error code or Python error class and allow the remaining regions to continue.
Any regional failure makes the command exit with status 1.
The command uses only `GetServiceQuota` and `ListRequestedServiceQuotaChangeHistoryByQuota`, including every history page.
It makes no quota request, paid API call, or local state write.
The administrator IAM action list in the runbook already includes both read actions.
`down` searches configured regions and regions in the saved manifest and stack records.
It terminates boxes before it deletes their regional stacks.
Then it deletes copied images and their snapshots.
`remove-images` changes the home image and keeps regional stacks and copied images.
Copied image records permit cleanup after an interrupted deployment.

## GPU size trial

`gpu_size_trial.py` provides local preparation, a transient scaler service, cleanup, and local comparison.
The session prepares and compares the trial without AWS calls:

```sh
python3 -m scripts.aws_target.gpu_size_trial prepare --commit SHA
python3 -m scripts.aws_target.gpu_size_trial compare --trial-folder DIR
```

The user installs the policy and starts the service:

```sh
python3 scripts/aws_target/deploy.py up --workload merge-gates --approve-resources
python3 scripts/aws_target/deploy.py gpu-size-trial run --trial-folder DIR --approve-resources
```

Preparation uses build admission and natedev holds.
It builds one archive and one NixOS GPU system closure.
The Rust tools and nextest come from Nix store paths.
The Hana toolchain output takes precedence when it exists.
Otherwise, the pinned host nixpkgs supplies Rust through its locked overlay.
A Hana `rust-toolchain.toml` selects the Rust version when that file exists.
Preparation uses a private Cargo cache.
It never copies the user's Rust or Cargo directories.
It checks toolchain wrappers for home paths and tests Cargo with an empty `HOME`.
Preparation builds all features and discovers shared libraries in the Cargo target directory.
A nextest archive profile inherits `ci` and includes each shared library through `archive.include`.
The archive retains the workspace's existing archive includes.
Nextest lists the extracted tests before preparation builds the NixOS closure.
An archive validation error includes nextest's diagnostic, which names a missing shared library.
Each measurement run creates its extraction directory before passing it to nextest through `--extract-to`.
Nextest 0.9.143 returns setup exit 96 when that directory does not exist.
A failed preparation copies up to 200 final lines of each failed `nextest.log` into `prepare.log`.
The reader seeks backward in 64 KiB chunks and reads at most 1 MiB per log.
It retains diagnostics when a run has no valid status record.
It then keeps only `prepare.log` and removes the other files and GC root links.
Host trial folders must be canonical direct children of `~/.local/state/gpu-size-trial`.
The commands refuse nested paths, outside paths, and paths through directory links before deletion.
A new preparation removes earlier trial folders under `~/.local/state/gpu-size-trial` when no live run holds their locks.
It refuses to create another folder while a live run holds an earlier folder.
Preparation, its worker, the service launcher, and the online worker hold folder locks.
The locks remain outside the trial folders.
A successful preparation keeps only these inputs and records:

1. `hana-tests.tar.zst`, `closure.export`, and `source.tar`.
2. `cargo-home/`, `runtime.env`, `gpu-trial.nix`, and `gpu_trial_wrapper.py`.
3. `closure.json`, `request.json`, `manifest.json`, and `inventory.json`.
4. `machines/` with baseline reports, samples, and logs, plus `prepare.log`.

After the archive and closure exist, preparation replaces the build tree with the source tar's clean contents.
It removes that source copy and the temporary Nix profile links after baseline measurement.
Each measurement removes its extraction directory, source copy, and Cargo target directory when the run ends, including failures.
Reports, samples, and logs remain.
The measured inputs imply an estimated peak near 32 GiB during preparation and about 12 GiB after preparation.
Local measurement needs about 19 GiB because only one extracted archive remains at a time.
These estimates cover the trial folder; Nix store use is separate.

The service checks candidate regions in their configured order.
It selects the first region whose applied G and VT Spot quota fits the largest type the trial launches (`gpu_size_trial_spot_max_usd_per_hour`).
It uses the existing type-size vCPU calculation and refuses a type whose vCPU count is unknown.
If no region fits, the refusal reports the required vCPUs and each region's applied value.
The quota request size remains separate from this selection rule.
It checks regional offerings and current Spot prices.
It uses the regional gate template and its NixOS image.
A separate policy statement permits the configured GPU types with the existing tags and a `GpuSizeTrial` tag.
The CPU gate statement remains unchanged.

Each box imports the signed closure download and reboots once.
The prepared system enables NVIDIA Vulkan and starts the trial runner.
The runner uploads each run's report and memory rows separately.
The wrapper records `ru_maxrss` for the largest single process of each test.
The report excludes GPU memory.

The ledger stores the mode, trial budget, and root size with each reservation.
The USD6 check includes completed costs and full open reservations under the accounting lock.
The daily and monthly limits still apply.
A restart retains the trial ID, limit, region, and type order.
A saved budget stop ends the trial.
After an online run ends, the worker saves its result or error before removing the archive, closure export, source tar, and Cargo cache.
It removes the temporary upload payload after upload, including an upload failure.
Metadata, results, and logs remain for comparison.
A restart uses saved hashes and uploaded inputs when upload completed.
A failure before upload requires a new preparation.

The service confirms termination before another launch.
The guest stops five minutes before the lifetime ends.
The expiry tag triggers two minutes before the lifetime ends.
`ExecStopPost` cleans known boxes and uncertain launches.
It saves an error and removes the large inputs if an interrupted worker leaves no final result.
The service uses no new signal handler.

`gpu_size_trial_root_gib=100` is an estimated disk requirement.
It includes the GPU driver, runtime, archive extraction, and two test runs.
The service reserves the configured maximum compute cost plus `gpu_size_trial_box_extra_usd` for each box.
The default extra is USD0.05.


### Hana setup configuration

Existing configuration values remain unchanged.

| New key | Value | Reason |
| --- | --- | --- |
| `stack_name` | `hana-build` | Keep Hana resources separate from gate resources. |
| `availability_zone` | `us-east-2b` | Keep the retained disk in one zone. |
| `ami_id` | Empty | Require the reviewed Hana safety image. |
| `ami_owner_id` | Empty | Require the reviewed image owner. |
| `vpc_id` | Empty | Require an explicit network. |
| `subnet_id` | Empty | Require an explicit public subnet in the disk zone. |
| `aws_timeout_s` | `10` | Bound each provider and receiver request. |
| `control_url_seconds` | `3600` | Bound signed access for one boot. |
| `control_poll_seconds` | `1` | Read current permission before the watchdog interval ends. |


## Hana source transport

`hana_transport.SourceSender` captures exact source through `copies.capture_source`.
The copies private index includes staged, unstaged, deleted, and permitted untracked files.
`apply_changes` uses the same private tree capture.
The caller's index and refs remain unchanged.
The existing tree key and a second private capture detect content and executable mode changes.
Both capture paths refuse new Git links with `NestedRepository`.
The sender refuses unexpected ignored inputs, credential paths, Git links, and disallowed environment variables.
It sends no ignored files, HOME, or target output.

A frozen `SourceManifest` carries the repository key, private commit, bundle digest, signed URL, basis commits, command, features, environment, and toolchain digest.
Its JSON fits the receiver's 65536-byte control limit.
Each repository retains a bounded list of guest-acknowledged commits.
Each snapshot commit has no parent, so bundles exclude the caller's Git history and deleted credential blobs.
Later bundles exclude those commits and their reachable objects.
A typed `MissingBasis` answer clears that repository's basis before a complete bundle retry.
`retry_complete` retains the captured commit even when the caller changes working files.

`AwsSourceObjects` implements the typed object boundary through the existing provider.
It uses conditional `PutObject` and signed `GetObject` access under `source/<deployment>/`.
The scaler policy grants only `s3:PutObject` and `s3:GetObject` for that deployment's source prefix.
Each AWS call uses `aws_timeout_s`.
The guest receives no AWS credentials.

`hana_source.materialize` bounds the download, verifies sha256 and Git prerequisites, checks the advertised commit, and fetches verified objects.
Curl enforces the total download timeout and maximum file size.
The data disk retains one bare mirror for each repository under `/var/lib/hana/mirrors`.
The shared maker allocates mirror metadata and temporary bundle files.
`build_slots.take_slot` supplies the only working checkout and retains its lease through the caller's context.
The slot API provides rescue before reset, stable Cargo output, and existing capacity refusal.
The slot's `rust-toolchain.toml` digest must equal the manifest before execution.

The guest must use host name `aws-hana`.
Admission requires `roots.slots.aws-hana`, `slots.aws-hana.hana.max`, and `slots.aws-hana.hana.max_bytes`.
Those values remain unset until the user approves them.
The guest image needs Git, curl, Python, `hana_source`, and the shared maker, slot, rescue, and configuration modules.
Phase F2 will deliver manifests and execute commands with inherited slot locks.

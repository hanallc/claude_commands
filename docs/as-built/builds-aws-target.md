# AWS cloud gates and trials

## Current model

AWS runs temporary Spot boxes for merge gates, paired AWS trials, and the GPU size trial.
The configured build target order is `hana-aws,mac,natedev`.
Hana placement has been enabled since 2026-11-17.
The queue tries `hana-aws` first for eligible Hana check and clippy steps.
The former permanent `aws-build` target is absent.
Cloud gates use `~/.local/state/build-targets/aws` without an `aws` target registration.
`regions` in `cloud_gate.conf` holds all 19 regions of `spot_candidate_regions`.
The saved deployment record identifies the regions available for launch.
Boxes boot the official NixOS image and fetch their tools at boot.
GPU trial boxes then import the prepared GPU system and reboot.

`config/aws_target.conf` supplies the account spending caps and billing settings.
`config/cloud_gate.conf` supplies launch, expiry, trial, and artifact settings.
The ledger reserves spending before launch and records confirmed termination.
The configuration files remain the source for cap values.

The spending guard reads `notification_recipient` and `notification_timeout_s` for each limit notice it attempts to send.
The ledger keeps the recipient and timeout from when the guard created the notice.
Each notice names the day or month that reached the limit.
The guard retries a failed notice after its saved retry deadline if the accounting period is still open.
The guard rounds the saved retry deadline up to a whole second for older readers.
When a notice's period ends in `accounting_zone`, the guard marks the pending notice as expired on its next check.
The ledger records an expired notice as `"delivery": "delivered"` with `"expired": true`.
Older readers treat the record as delivered.
A retry after a recipient change can send the same notice to both recipients. A duplicate notice is safer than a lost notice.
Code older than this change still sends a pending notice after its period ends. That ends when every branch has this change.

Floors from Eastern accounting days before the UTC change can overstate the November 2026 month count by at most USD 1.99.
These floors leave the month window on December 1, 2026.
A legacy notice ID expires when its period ends.
The code has no special handling for these cases.

`report.py` reads the AWS state folder without target registration.
Its AWS section retains the account bill, daily gate spend, and historical builder comparisons.
The AWS lead line uses one UTC accounting day for cost, uptime, steps, and savings.
An explicit report date selects the UTC day with that date.
A report without a date uses the UTC day at the report time.
`sync.hosts.aws=aws-build` identifies historical steps.
The report shows no synchronization status for the removed builder.

One resolver selects the scaler credential file for deployment, services, key creation, and teardown.
It uses a nonempty `AWS_SHARED_CREDENTIALS_FILE` override before `credential_file` from `cloud_gate.conf`.
It expands `~` in either path.
Deployment uses encrypted administrator credentials for resource changes.
Gate services use the private scaler profile.
Boxes receive signed URLs and no scaler credential file.

## AWS plan identity

Run this command in a terminal on natedev.

```sh
deploy.py plan-identity --approve-resources
```

For a new key, the command checks encryption to the GPG key that protects the GitHub token.
Run `github-warmup` in a terminal on natedev if the GPG agent is cold.
Enter the administrator passphrase when the command asks for it.
The command creates the `aws-build-plan` IAM user and one managed policy.
It checks the user with STS, reads EC2 availability zones, and simulates seven denied actions.
After these checks pass, it saves one access key in `~/.config/aws-build/plan.gpg` with mode `0600`.
If STS still rejects a new key after retries, setup revokes the unpublished key.
Run `deploy.py plan-identity --approve-resources` again after one minute.
If setup fails before it publishes `plan.gpg`, it revokes any new key it finds.
During setup, `plan.gpg` must be absent before the command revokes a key.
If directory sync or cleanup fails after setup publishes a verified key, the command warns and reports that the identity is ready.
If IAM lists no key after a failed creation, a key can appear later.
Run the same approved command again to revoke the late key and create a new key.
If `plan.gpg` is absent, the command revokes all keys that IAM lists for the plan user before it creates one key.
Setup keeps the local key when it matches the sole key that IAM lists.
Setup refuses if IAM lists no key, a different key, or more than one key.
If STS rejects a saved key, setup preserves the local file and the IAM key.
Setup also refuses if it cannot read or decrypt `plan.gpg`.
This refusal preserves the local file and the IAM keys.
Setup uses GPG without a passphrase prompt for these checks.
The command compares the default policy with the current configuration and sets a new default version when they differ.
Run `deploy.py plan-identity` without flags to print the plan without contacting AWS.
Run `deploy.py plan-identity --check` to verify the saved plan key with STS without a prompt.
Setup and the check use the region in the AWS target configuration.
If AWS does not answer the check, run the check again.
If AWS rejects the saved key, run the approved removal command below.
Use the same command if `plan.gpg` is damaged or has invalid fields.
Then run `deploy.py plan-identity --approve-resources` to create a new key.

The policy allows setup reads, IAM simulation for users in the account, and S3 object listings in account buckets.
The policy allows reads of applied service quotas and quota request history.
The command prints the configured names and regions of the home, regional gate, and Hana stacks.
The policy limits CloudFormation stack details and templates to those stacks.
It allows creation and deletion of change sets named `aws-build-plan-*` on those stacks.
The policy blocks the `TemplateURL` request parameter.
CloudFormation accepts an inline template body or `UsePreviousTemplate` when the stack has a template.
A `CREATE` change set for an absent stack leaves an empty stack in `REVIEW_IN_PROGRESS`.
The policy denies deletion of every stack.
It denies change set execution, role passing, EC2, IAM and S3 writes, and S3 object reads.
It denies EC2 instance attribute and Spot request reads because those calls can return user data.

Sessions use `plan_credentials()` to decrypt the key without a prompt.
If the agent is cold, run `github-warmup` in a terminal on natedev.
The approved removal command removes the IAM resources before it removes `plan.gpg`.
It removes `plan.gpg` even if it cannot decode the file.
It refuses if the decoded key belongs to a different AWS account.

```sh
deploy.py down --workload plan-identity --approve-destroy
```

## AWS test identity

1. Sign in to the AWS console for the management account.
2. Switch to `OrganizationAccountAccessRole` in the test account.
3. Create the IAM user `aws-build-test` with the path `/aws-build/` and the `AdministratorAccess` policy in the test account.
4. Create an access key for that user.
5. Run `deploy.py test-identity` in a terminal on natedev.

Enter the access key ID and the secret access key at the hidden prompts.
The command checks the key format before it calls AWS.
It uses STS with the saved plan key to identify the build account.
It uses the region in the AWS target configuration for this STS call.
It refuses a build account key or a test key whose account it cannot confirm.
It stores the key in `~/.config/aws-build/test.gpg` with mode `0600`.
The local AWS state folder holds the account ID in `test-identity.json`.
The command saves the account record before it publishes the encrypted key.
If setup stops after it saves the record, run the command again with a key from the same account.
Run `github-warmup` in a terminal on natedev if the GPG agent is cold.
If GPG cannot identify the encryption recipient, restore `~/.config/gh/token.gpg` with one GPG recipient.

Run `deploy.py test-identity` again with a key from the same account to replace the key.
The command refuses a key from a different account.
Run `deploy.py test-identity --check` to verify the saved key and account without a prompt.
If AWS does not answer, run the same command again.
If you interrupt the check, run the check again.
If the test key is locked, run `github-warmup`.
If the private test key is unreadable, run `deploy.py test-identity` with a key from the recorded test account.
`test_credentials()` checks the saved test key against the recorded account with one STS call.

## Run a setup plan

Run this command in a terminal on natedev.

```sh
deploy.py hana-setup --plan
```

The plan reports whether the deployed Hana image is current or stale.
The plan checks root replacement for an existing Hana box with a registered image.
It accepts stack-owned and direct-owned boxes.
It refuses first-time setup and pending instance or data volume work.
The plan refuses immediately if the controller lock is busy or another command holds Hana.
A new replacement requires an idle lifecycle.

For a pending replacement, the plan checks the instance, original data attachment, and registered image.
For a new replacement, the plan also checks that the instance is stopped.
The policy preview checks the local credential file and reads the scaler identity with STS.
It reads the live stack and previews one modification to `ScalerPolicy` through IAM simulation.
The plan makes read calls only.

The IAM simulation excludes the live `ScalerPolicy` and includes the proposed document.
The plan simulates the EC2 probes without reading user data or starting the box.
The S3 simulation uses the control bucket policy from the stack template.
If the template has no policy or the plan cannot resolve it, the plan prints a note.
The plan then simulates S3 without a bucket policy.
Bucket policies added outside the stack are out of scope.

To plan a merge gate update, run this command in a terminal on natedev.

```sh
deploy.py up --workload merge-gates --plan
```

The plan checks the home StackId, account, region, deployment parameters, and installed scaler credentials.
The gate plan, approved gate setup, and image removal take the account stack lock at `gate-up/controller.lock`.
This lock protects the account stack, regional gate stacks, and `gate-deployment.json`.
Account apply and teardown take the account stack lock before the Hana resource hold.
A command refuses before it contacts AWS if another command holds the account stack lock.
The gate commands leave Hana build claims and permit renewal available.
It builds the same template and parameter values as approved gate setup.
The plan previews updates in the home region and in each configured gate region.
It removes each change set and confirms removal before it checks scaler permissions.
Each removal has one deadline for all deletion and confirmation calls.
An absent stack refuses because a CREATE change set can leave a stack in `REVIEW_IN_PROGRESS`.
The plan identity has no permission to delete a stack.
An existing stack in `REVIEW_IN_PROGRESS` also refuses.
In a region with a rolled-back first create, the plan checks scaler permissions.
Approved setup must delete and recreate that regional stack.

The plan reads the change sets for managed policies that the updates remove or replace.
If a preview fails or its removal is unconfirmed, all regions that share the scaler policies refuse.
The simulations exclude those policies and the live versions of the proposed gate scaler policies.
If the plan cannot identify a policy to exclude, it names the policy and refuses.
The simulations include the proposed gate policies and the proposed S3 bucket policy.
The simulations use the regional VPC, image, and Spot request conditions.
The plan simulates `ec2:GetSpotPlacementScores` in each gate region.
The plan reports each region in configuration order.
It passes when every region passes.

## Hana guest safety files

`scripts/aws_target/hana_guest.py` renders safety files for the kept Hana box.
`config/hana_aws.conf` supplies the image's admission setting, safety timing, and resource values.

`scripts/aws_target/hana_render.py` renders the safety and receiver entry points, settings, and systemd units.
It applies each rendered file's mode.
The image keeps `safety.py`, `receiver.py`, `safety.json`, `receiver.json`, and their units.
It installs the Python closure in `scripts/aws_target/hana_image.files` under `/var/lib/hana-runtime`.
That closure supplies the root receiver, the safety code, and the unprivileged bootstrap.
NixOS supplies `hana-tools.service`, the runner environment, the compiler version, and the approved slot settings.

The NixOS build runs the renderer from its pinned `claude-commands` input.
The NixOS image requires these integration changes.

1. Update the `claude-commands` pin to include the per-run runtime change.
2. Include the paths listed in `scripts/aws_target/hana_image.files` and these seven files in the renderer input filter.
   - `config/build_folders.conf`
   - `config/build_targets.conf`
   - `config/hana_aws.conf`
   - `scripts/aws_target/hana_image.files`
   - `scripts/aws_target/hana_render.py`
   - `scripts/lib/conf.py`
   - `scripts/lib/conf_reads.py`
3. Install the rendered `runtime/` at `/var/lib/hana-runtime`.
4. Append the approved roots and capacity to the rendered `runtime/config/build_folders.conf`.
5. Put the approved `# whole:` limit directly above each capacity setting.
6. Give the bootstrap write access to `/var/lib/hana/tmp`.
7. Provide Python 3.13 or newer for the bootstrap.
8. Remove `CONF_DIR` from `runner-environment`.

Root and the bootstrap read the installed `build_folders.conf`.
User data supplies the boot descriptor and signed control access.

Run this command from the repository root:

```sh
python3 -E -m scripts.aws_target.hana_render --device /dev/sdf <out_dir>
```

`-E` ignores `PYTHONPATH`.

The boot guard waits for a fresh permit within `guard_grace_seconds` from boot.
It has no ordering or requirement dependency on the data disk, network, or tool setup.
Tool setup runs after the guard and requires the bounded data mount.
A failed setup or missing disk cannot prevent the guard from requesting poweroff.
The mount bounds both device discovery and mount execution through `mount_timeout_seconds`.

The receiver service runs as root with `UMask=0077`.
The receiver installs the private, root-owned record `/run/hana-aws/active-work-permit.json`.
The record holds `expires_at` in UTC epoch seconds, `generation`, and `boot_id`.
The boot ID must match `/proc/sys/kernel/random/boot_id`.
The receiver uses a private temporary file and an atomic rename under the shared lock.
The receiver acknowledges a new permit after the rename succeeds.
The guard, watchdog, and command admission read the same record for the permit and generation.
The receiver, guard, and watchdog ignore the legacy `work-permit.json` and `generation` files.
A folder with only legacy permit files starts with no installed permit and expects a prior generation of zero.
Missing, unreadable, malformed, expired, or mismatched permits grant no permission.
`permit_max_seconds=1800` bounds one renewal window.
A permit beyond that window grants no permission.
The controller can renew permission for longer steps.

On the first read, safety records the permit's boot-clock deadline in `/run/hana-aws/permit-deadline.json`.
An unchanged permit retains that deadline across later reads and service restarts.
A renewal with a changed expiry records a new bounded deadline when safety next reads it.
The watchdog stops at the earlier of wall-clock expiry and the boot-clock deadline.
A backward wall-clock change cannot extend the boot-clock deadline.
Command admission reports disabled guest settings, a missing data mount, or an invalid permit separately.
The receiver sends a refusal code with the diagnostic text.
The permit refusal names the failed identity, expiry, or deadline check.
An invalid or missing expiry field in a permit with valid identity fields gets a specific expiry refusal.

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
The receiver checks the actual data mount before it starts the command through this path.
`test_hana_guest.py` tests the decisions with injected clocks and verifies rendered units when `systemd-analyze` exists.
No safety test needs AWS or a running systemd manager.

## Hana spending permits

`scripts/aws_target/hana_spend.py` reserves spending before it returns a guest permit.
`HanaSpending` uses the account ledger lock for starts, renewals, lifecycle observations, and storage deletion.
It makes no AWS call and delivers no permit to a guest.
The lifecycle controller delivers permits through signed control and reconciles the box without an active caller.

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
| `instance_type` | `m7a.4xlarge` | Provide 64 GiB of memory. |
| `fallback_instance_types` | `c7a.4xlarge,m7i.4xlarge,m6a.4xlarge` | Try these types when the first choice cannot launch. |
| `spot_max_usd_per_hour` | `0.35` | Bound the compute reservation and skip higher Spot prices during setup. |
| `spot_ceiling_margin` | `0.25` | Add this positive fraction of the current Spot price when setup suggests a new ceiling. |
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
| Idle-stopped | A confirmed idle or interruption recovery stop permits a later demand start. |
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
A confirmed interruption takes priority over a pending stop.
The controller records `INTERRUPTED` and refuses a manual restart.
A stopped observation with an unknown cause preserves `INTERRUPTED`.
If the controller stops an unclaimed restart, it saves its stop decision before it calls the provider.
The decision records the generation and whether an interruption came before the restart.
Another running observation keeps the stop in progress.
If the running box enters a new generation, the controller saves a new stop decision for that generation.
After compute exposure closes, a fresh stopped observation settles the unclaimed stop decision for the same generation.
The controller records `IDLE_STOPPED` if that decision followed an interruption.
A stopped observation from a different generation preserves `INTERRUPTED` if the old decision followed an interruption.
An unclaimed stop with no prior interruption ends at `STOPPED_UNKNOWN`.
The decision applies whether the controller or the guest stops the box.

The controller cannot use a saved `STOPPED_UNKNOWN` row to infer a Spot interruption from the next compute generation.
The compute ledger records exposure times, but it lacks the last running observation and the reason for the stop.
The exposure record does not identify a guard window or a reservation.
The provider records lack the generation and the acceptance time of a start or stop.
An EC2 restart and a start in the AWS console can produce the same saved records.

After you confirm Hana is stopped, run `deploy.py hana-release-unknown-stop` to preview the release.
With `--approve-release`, the command saves a stop decision and records the approval time.
The command requires `STOPPED_UNKNOWN` and closed compute exposure in the newest ledger generation.
A claim, pending operation, saved demand start, or replacement duty prevents approval.
The command makes no AWS call and needs no AWS credentials.
Hana remains in `STOPPING` and refuses claims until the controller confirms the stop.
If the controller finds Hana running, it saves a stop decision for the running generation and stops Hana.
The controller sets `IDLE_STOPPED` after a fresh stopped observation with closed compute exposure for the stop decision.
Repeat the command to check whether the release waits for confirmation or is already complete.
Later claims use the usual image, spending, and permit checks.

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
A failed install leaves the active permit unchanged.
An exact replay can acknowledge the installed permit while it is fresh.
The controller reserves exposure until stop confirmation through the Phase C ledger.
It reconciles automatic restarts without a caller, including while placement is disabled.
An automatic restart cannot reuse an interrupted execution's boot challenge.

`HanaProvider.step()` defines the typed provider boundary.
A provider must bound its calls and use the saved operation tokens.
Its `recover()` method reads status without repeating a mutation.
It must distinguish a completed request from an unresolved request before reporting `MutationSettled`.
The provider refreshes bootstrap access before a demand start.
It must verify root receiver responses against deployment and demand-start identities.
`hana_provider.AwsHanaProvider` supplies the concrete AWS provider.
The controller constructs it with the scaler profile and a sanitized credential environment.
An unprovisioned tick makes no provider call.
`Deployment.prepare_hana` and `Deployment.hana_setup` in `deploy.py` prepare and provision a separate Hana stack through the existing deployment helpers.
The user runs `prepare --workload hana`, then `hana-setup --approve-resources`.
Without approval or `--plan`, `hana-setup` prints a local resource summary.
The existing preparation writer, preflight verifier, encrypted admin executor, stack waiter, and progress display serve both workloads.
The separate template is `docs/plans/builds-aws/hana-cloudformation.json`.
The stack creates the launch template, security group, private control bucket, and scaler policy.
The launch template uses persistent Spot and stops the instance when AWS interrupts it.
Before a launch, setup reads Spot prices for the configured type and its fallback types.
If the instance is already recorded, setup resumes without a Spot price check.
It checks the registered subnet and each available default subnet in another zone of the same VPC.
Setup prints one reason for each candidate with no price or a price above the ceiling.
If the price read fails or no candidate qualifies, setup exits before it changes the stack.
If all priced candidates exceed the ceiling, setup shows the cheapest candidate.
Setup shows a `/tune` command when the effective ceiling key is editable.
If the configuration rejects the command, setup reports the error and the suggested ceiling.
The suggested ceiling includes `spot_ceiling_margin` and rounds up to a cent.
It reserves setup costs within both account and workload limits before the stack request.
`reserve_setup()` reserves compute for approved resource setup.
Demand starts and guest command admission require their own authorization.
Setup uses the existing stack after a launch failure.
After all launch requests receive a refusal, prepared setup can change the ceiling on that stack.
Setup reserves compute at the new ceiling and saves the launch template version before it attempts the next launch.
It tries the configured type and registered subnet first if their price qualifies.
It tries the other candidates in order of price, starting with the lowest.
Setup prints the type, zone, price, and ceiling for each launch attempt.
A capacity or price refusal moves setup to the next candidate.
If all attempts fail, setup reports each reason and keeps the stack for the next run.
An unexpected error stops setup.
Setup saves a client token before each launch and reuses it if the outcome is unknown.
It refuses changed resource inputs or an expired reservation for a pending launch.
Setup saves the returned instance identity, Spot request identity, and zone in `hana/setup.json` before it waits.
Setup saves a client token before it creates an encrypted gp3 data disk in the launch zone.
The AWS CLI makes one attempt per launch or disk creation command.
Setup clears a new intent when EC2 confirms that it rejected the first request.
Setup keeps the intent when the outcome of an earlier attempt is unknown.
Setup saves the disk ID before it waits for the disk to become available.
Setup reads the instance state again before it attaches the disk at `/dev/sdf`.
Setup permits attachment when the instance is running or stopped.
If AWS confirms that the stack is missing, setup checks the record for resources or pending requests.
If the record names either, setup asks the user to run `down --workload hana --approve-destroy`.
Otherwise, setup searches the deleted stack history for the saved stack name.
It uses stacks that AWS created at or after the saved reservation time.
Setup reads the events of each matching stack by ID to find retained disks and buckets.
It ends Spot boxes with the saved deployment tag and deletes the retained resources before it clears the reservation and record.
If it cannot verify cleanup, setup keeps the record and asks the user to run `down --workload hana --approve-destroy`.
Setup verifies the persistent Spot request before it requests the idle stop.
It checks the instance state every `stop_poll_seconds` (10 seconds by default).
Each check reports the state and elapsed time and uses the `aws_timeout_s` command limit.
Setup starts each check within `stop_wait_timeout_s` (600 seconds by default).
A check can finish after the wait limit.
If the wait expires, run `deploy.py hana-setup --approve-resources` again to resume the stop.
Setup makes no further AWS calls after the stop wait fails.
Setup keeps the pending stop and accepts expired preparation if the inputs still match.
It registers the deployment after it confirms the stopped state.

Setup then probes the scaler path with the same credential file and profile as `aws-target.service`.
It checks credential export and reads the instance, Spot request, and user data.
It uses EC2 `--dry-run` for the user data change, start, and stop commands.
AWS confirms permission for these commands with `DryRunOperation`. [AWS CLI dry-run reference](https://docs.aws.amazon.com/cli/latest/reference/ec2/modify-instance-attribute.html#options)
The user data dry run uses the value from the user data read, or an empty value if the read fails.
Setup writes a fixed probe object under `control/<deployment>/setup-probe.json` and compares the bytes it reads back.
It reports each operation as `hana-setup probe: <operation>: pass` or `hana-setup probe: <operation>: fail`.
When AWS supplies an error code, the failure line includes it in parentheses.
For a policy-only change, setup probes before and after the update.
Approved setup waits for an idle-stopped lifecycle with no claim or pending start.
It makes no AWS write calls while it waits.
The default setup wait is 1,500 seconds.
This allows time for work to finish, the 600-second warm idle interval, and the 600-second stop wait.
Set `setup_idle_wait_timeout_s` above the sum of `idle_stop_seconds` and `stop_wait_timeout_s`.
The `setup_idle_heartbeat_s` setting reports the lifecycle state every 15 seconds.
Setup checks the lifecycle again at the wait limit.
If the box is still busy, setup reports the lifecycle state and the claim holder kind.
Setup takes exclusive control of the box under the controller lock.
The setup plan, Hana image registration, and Hana teardown use the same hold.
Account prepare, Hana prepare, account apply, and account teardown also use the hold.
The controller skips its tick while a command holds Hana resources.
A competing command refuses before it calls AWS.
Build claims and queue evidence report the box as busy during the hold.
The kernel releases the hold when the command closes it or the process exits.
It checks the saved stack outputs and confirms the instance is stopped and the disk is attached.
Setup verifies the live and effective change set parameters against the approved stack parameters.
The second run starts after CloudFormation confirms the update.
Setup continues the update if the first probe run fails.
Setup returns exit status 1 if any operation in the last probe run fails.
Setup adds no route, target, or slot capacity.
Setup uses a change set to change the stack.
For a registered version-1 box, setup reads the original stack template and replaces only `ScalerPolicy`.
The change set uses the previous value of every live parameter.
The instance, data disk, and attachment stay in the stack.
Setup saves the updated template hash and keeps the version-1 record.
If the policy is current, setup runs the probe without executing a change set.
Setup refuses a stack-owned box if it cannot confirm the saved resource identities.
Setup refuses a change set that replaces or removes an instance, volume, or attachment.
When a policy update fails, setup prints the failed step and the update error.
When an ordinary stack change fails, setup reads failed stack events from the current change before cleanup.
The error gives the name, type, status, and AWS reason for each failed resource.
If setup cannot read the events, it reports that they are unavailable and continues cleanup.
Setup ends the Spot boxes other than the box in `hana/setup.json`.
After a failed create, setup requires `ROLLBACK_COMPLETE` or `ROLLBACK_FAILED` before it deletes the stack.
It waits for stack deletion and reads events by stack ID for disks and buckets marked `DELETE_SKIPPED`.
It deletes these resources before it closes the spending reservation and removes `hana/setup.json`.
If the rollback or cleanup is incomplete, setup keeps the record and reservation for `down`.
For an incomplete setup, `down` also checks deleted stack history for retained resources.
If that history has no match, `down` reports it and continues with any disk or bucket in the saved record.
After a failed ordinary setup update, setup deletes resources retained during rollback and keeps the stack and record.
Run `prepare --workload hana` again before retrying with changed inputs.
The launch template sets `DeleteOnTermination` on the root disk. AWS deletes each root disk with its box.

### Hana image freshness

`hana_render` hashes the paths, modes, and bytes of the rendered image files.
It adds the digest to the rendered `safety.json`.
Safety settings, receiver settings, the command timeout, and the slot probe settings affect the digest.
The renderer selects overrides by the Hana alias in `hana_aws.conf` and writes plain keys.
The renderer rejects invalid slot probe settings before it computes the digest.
NixOS supplies the approved slot roots and capacity, compiler version, and runner environment.
Those NixOS settings take effect with a new image.

An edit to a runtime-only file leaves the image digest unchanged.
The receiver still uses `enabled` as an independent image admission switch.
This split requires one image replacement before the box accepts a combined payload.

`hana-image` reads the digest from the renderer output in the built image's Nix derivation closure.
Registration requires the build derivation and the renderer output to be present in the local store.
The registered image description and the local registration record carry the same digest.
Setup records the digest for the instance after EC2 confirms its image identity.
Registering another image does not refresh the digest of an existing instance.
A missing or mismatched digest makes the deployed image stale.
An invalid image setting also makes the deployed image stale.

The queue excludes a stale Hana box before assignment.
The controller checks the image before accepting a claim, starting the box, or granting command permission.
A stale image detected after assignment records `HanaCommandAdmissionRefused` with `DoNotRetry`.
The queue can place that build on another eligible target after it settles the Hana attempt.
The build and target status report this message.

> The Hana image is stale. Run deploy.py hana-setup --plan to review replacement.

After it saves an effective image setting, `tune.py` reports this message.

> The Hana image is now stale. Build and register a current Hana image.

The root replacement procedure keeps the same instance and data volume. [Replace the Hana root image](#replace-the-hana-root-image)

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
`deploy.py down --workload hana --approve-destroy` cancels the Spot request and confirms that the instance terminates.
Before deletion, teardown searches for a pending launch by client token and a pending disk creation by tag.
Teardown repeats each search for up to `intent_lookup_timeout_s` (120 seconds by default).
If a search expires, teardown preserves the stack, charges, and setup record.
It deletes the data disk before it deletes the stack.
It then deletes the retained control bucket.
For a legacy stack, it deletes the stack first to remove the managed disk attachment.
It closes the compute and storage charges before it removes the local Hana records.
The `--approve-destroy` flag authorizes deletion of the disk and bucket data.
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
Small provider requests use `aws_timeout_s`.
Source uploads and result downloads have separate limits.
Provider errors expose safe action names rather than cloud identifiers or signed URLs.

The provider persists its operation before dispatch and holds a separate mutation lock.
Existing writes store the lifecycle generation and UTC epoch seconds for dispatch, acceptance, or cancellation.
Acceptance writes identify a direct response or a bootstrap observation.
The scaler gives `StartInstances` one attempt.
The provider saves a definite service refusal as `start_refusal_code` before returning the error.
The record contains the code only.
A later active observation takes precedence over the refusal for accounting.
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

Fixtures in `scripts/aws_target/fixtures/` supply settings for `aws_target` tests.
The tests do not read tracked configuration.

## Removed builder: 2026-11-10

`builder-removal.json` records the removal. Commit `1b1fa1842` removed the target on 2026-11-10.
The persistent `aws-build` instance, Tailscale device, and data volumes were removed.
`routing.json` records `enabled: false`.
The registry no longer includes the former `aws` target.
The ledger and teardown retain historical records for the former builder.
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
The gate box passes signed URLs to curl in private files. A transfer failure reports the stage, curl exit code, and HTTP status.
The bootstrap removes each known signed URL and its query from the result records and logs it sends.
Older paired candidates can send logs directly. The bootstrap cannot filter those uploads.
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
With `enabled=off` in `cloud_gate.conf`, the route sends merge gates and targeted tests to natedev.
For each request, the route reads this setting before it reads deployment state or calls AWS.
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
The gate box gets `packages` from `config/cloud_gate.conf` at the deployment host's locked nixpkgs revision.
The list includes bubblewrap.
Before `guest.py` starts, the boot script runs `bwrap --ro-bind / / true` as `gate-trial`.
Success writes `stage=bwrap ready` to the boot log.
Failure reports that bwrap or unprivileged user namespaces do not work for `gate-trial`.
The boot log and the boot error record include this reason.
A failed check prevents the gate box from running tests.
The production EXIT handler attempts to send any boot error before it requests poweroff.
The handler requests poweroff and keeps the boot session exit status even if it cannot write the boot error record.
Cloud gates and paired trials use this boot script.
GPU size trials use their own boot script and prepared system.
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
An unbilled day is idle only if every run exposure that starts before its end has a confirmed closure before its start.
An expired permit or reservation still counts until the ledger confirms closure.
The page, spending guard, and admission use the usual prices and bill carry on run days.
After the latest bill, an unbilled idle day carries retained resource costs and unknown usage types.
It skips usage linked to runs and uses the usual accounting for Cost Explorer fees.
The page, spending guard, and admission use the same idle-day rule.
The guard keeps any higher established charges.
The ledger and hours table exclude a closed Hana exposure only when its START record has a valid `start_refusal_code`.
A possible start or an observed acceptance in any generation of the exposure keeps the full exposure.
Legacy cancellations and stopped states retain the full exposure when the journal has no refusal code.
Reports keep the existing reconciliation floors.
The account checks adjust the existing total for each qualifying UTC day inside the limit window.
They subtract that day's existing usage and add the newest qualifying AWS amount.
An account bill qualifies when the name, day label, and bounds match one full UTC day.
The bill must exclude credits and give at least USD 0.01 before rounding.
The newest read time must be valid and cannot be in the future.
The charge file keeps each read time and amount. A legacy file without a list supplies one read.
Known limit. Concurrent refreshes can record one USD 0.01 fee for two Cost Explorer reads.
Within the lookback, the last `cost_explorer_unchanged_reads` reads after the UTC day ends must report the same amount.
Reads less than `cost_explorer_interval_seconds` apart count once.
A different amount starts a new count. The AWS service logs the day and both amounts when the day becomes unsettled.
Outside the lookback, the latest valid amount qualifies.
The adjustment preserves query fees and any open gate allowances or reservations already in the total.

Each scheduled Cost Explorer read requests the last `cost_explorer_watch_days=45` completed UTC days.
The watch must cover at least `cost_explorer_lookback_days=7`.
The reader makes one scheduled query each day.
It saves the lookback days before it saves the older days.
It logs a save error for an older day and continues with the other days.
The query groups costs and quantities by usage type.
AWS charges USD 0.01 for each page of the answer.
The 45-day answer can take more than one page.
The ledger records the actual fee for each read.
The watch derives the first settled amount from the first sequence of equal counted reads.
The AWS page shows how many recorded days still match their first settled amount.
It shows the date of the newest read in UTC and lists each day with a different current amount.
The history keeps earlier differences when the newest read returns to the first settled amount.
The watch sends one message for each read that finds changes that it has yet to report.
The message goes to the session in `cost_explorer_change_recipient=bob`.
It uses the page wording and includes one line for each changed day.
It checks all saved days, including days outside the query range.
After a successful send, each charge file records the reported amount.
A write error after delivery can cause the next message to repeat that day's line.
After a failed send, the next read collects and tries all pending changes again.

The report's costs pane must call `CostLedger.accrued_cost` for the current day or month to date.
The function returns `cost`, `limit_count`, and the cost and bill state for each UTC day in the window.
`cost` is the ledger estimate plus the same complete-day adjustment that the guard uses.
`limit_count` is the admission guard's value, equal to `cost` plus reserves and headroom.
The function reports an unavailable limit count when the admission guard cannot calculate it.
The per-day costs add up to `cost` and include separately named query fees and any rounding adjustment.
The pane displays a counted bill as “Billed, final” only when the bill sets `provisional` to JSON `false`.
The pane displays all other counted bills as “Billed, not final”.
The pane displays other days as “Estimate”.
An estimate can show the newest valid AWS amount in `aws_amount_so_far` while the day remains unsettled.
The tooltip labels this value “AWS amount so far”.
The pane's “Time on AWS” row reads `run_cost.hana_billed_hours` for one UTC day.
The reader returns billed Spot hours for Hana's configured region and machine types.
The file name, day label, and bounds must match the requested full UTC day.
A valid bill with no matching usage gives zero hours.
A missing or damaged bill gives no record.
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

`deploy.py up` always checks and updates the home stack.
For each additional region, it hashes the rendered template, stack name, and parameters passed to CloudFormation.
The deployment state folder stores each successful regional digest with the live stack ID and last update time in `regional-stack-digests.json`.
Deployment skips a stack when all three fields match and the live status is `CREATE_COMPLETE` or `UPDATE_COMPLETE`.
Deployment reads the outputs from that status reply and records the elapsed time for the region.
Deployment warns once if it cannot read the digest file or finds an invalid record.
It then deploys every region.
Each deployment call uses a private template file that it removes after the call.
Run one `deploy.py up` at a time. Two runs from different state folders can save a digest for the other run's stack revision, and a later run can then skip that region.

`buildlog.timer` runs the hourly Spot snapshot through `buildlog hourly` on natedev.
The existing timer uses `OnUnitInactiveSec=3600s` and calls the installed scripts.
The hourly job refreshes the Spot snapshot and placement scores. It also checks billed hours.
`python3 scripts/aws_target/aws_target.py snapshot` runs the same operations manually.
`aws_target.py tick` reconciles the Hana lifecycle and refreshes account charges.
`price_refresh_seconds` limits snapshot attempts to one per configured interval.
`deploy.py up` runs the same reader with `force=True` after the stack update, so prices fill at once.
A forced read does not move the interval, so the next hourly read still runs.
The hourly timer starts one hour after the last run ends, so two hourly reads are always more than 3600 s apart.
A failed regional read can leave older values in the snapshot.
It tries `deploy_price_fill_attempts=3` times, `deploy_price_fill_pause_s=10` seconds apart, while IAM shows the new policy.
The deploy prints the reasons and sends no alert; a failed fill does not fail the deploy, and the hourly refresh sends the alert.
The snapshot takes `spot-prices.lock` without blocking.
It reads each distinct `spot_candidate_regions` region once.
Each regional read requests all `instance_types` and `gpu_instance_types` prices for Linux/UNIX.
The AWS CLI reads all pages, and the reader selects the latest price for each type and zone.
The reader also reads applied Standard Spot and G and VT Spot quotas.
It uses the scaler `credentials_profile` and the private `credential_file` without an administrator passphrase.
It removes inherited administrator keys and profiles from the AWS process environment.
The snapshot calls `ec2:DescribeSpotPriceHistory` and `servicequotas:GetServiceQuota`.
The CLI service name is `service-quotas`; the IAM prefix is `servicequotas`.
See the [AWS authorization reference](https://docs.aws.amazon.com/service-authorization/latest/reference/list_service-quotas.html).
The snapshot test derives IAM actions from every request in the real snapshot path.
It checks the scaler policy for each action, all resources, and no regional condition.
The scaler policy also allows `ec2:GetSpotPlacementScores` on all resources.
The grant has no regional condition because a score request can name several target regions.
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
It supplies Spot prices and quotas for Standard and GPU. It calculates gate estimates only for Standard.
Each data table has one row per candidate region.
Spot prices use the lowest zone price in USD/h.
The snapshot retains prices and quotas from the last successful read.
The snapshot lines give the age and result of each region's last read.
A failure line gives the time, failed read, error class, and AWS error message.
A region without a snapshot says that it has no snapshot.
The standard types come from `instance_types` and `price_compare_instance_types`.
The GPU types come from `gpu_instance_types`.
The hourly snapshot reads all three lists.
Below the Prices and quotas card, the page shows one run table per standard type.
Each run table has one line per candidate region.
The user asked for this layout on 2026-11-14 through builds. Each line has these columns.

1. The median box time for the selected runs in minutes, from launch through finish, with one decimal such as `7.5`.
   Times come from `cloud-gate-launches.jsonl` over the last `run_cost_days` days.
2. Runs gives the number of runs used to calculate the median.
   A region without its own runs uses the type's runs in other regions, and the cell says `elsewhere`.
   A type without runs uses the runs of the types with its vCPU count, and the cell says `same vCPUs`.
3. Paid USD/h divides the bill's `SpotUsage:<type>` cost for the type and region by its billed hours.
4. Paid USD/run gives the selected runs' share of the bill, which splits between runs of both kinds by box time.
   The cell also counts the runs whose day has no bill yet, such as `0.0314 (17 bill due)`.
   A bill comes up to about a day late.
   A region whose runs all wait for a bill shows `17 bill due`; a region with no own runs of the type shows `no runs`.
5. Paid runs gives the number of runs that Paid USD/run covers.

The bill names the region by its usage type prefix, such as `USE2-SpotUsage:c7a.8xlarge`.
`ledger.REGION_USAGE_PREFIXES` maps each prefix to a region.
A launch row without a region ran in the home region, `region` in `aws_target.conf`.
The next line names the cheapest merge gate by estimate.
Below them, two tables show the refused requests and the launched boxes of each type and region.
Refused requests are capacity and quota refusals.
The last card asks “Did AWS bill more machine hours than we used?”
It uses `run_cost.hours_checks` over the days with a bill in the configured window.
When a higher bill exceeds the limit, the answer sums the displayed differences for those rows and gives their count.
If only lower bills exceed the limit, the answer sums the displayed differences for those rows and gives their count.
If all comparisons pass, the answer gives the total billed hours and the total recorded hours.
If the answer is “No” and the displayed billed total is higher, a second sentence explains the limit and the Hana allowance.
The visible table includes only higher bills that exceed the limit.
The closed “All rows” control opens a table for every type and region with hours in the selected regions.
The tables show billed hours, recorded hours and the signed difference to one decimal place.
The difference subtracts the full values before rounding.
If a marked difference or its sum rounds to zero, it shows as “less than 0.1 hours.”
The Hana reserved column shows the hours set aside for Hana where the check uses them.
If the bill for a row is due, the billed hours and the difference show a dash in “All rows.”
The shared region filter includes all regions with an hours check.

The AWS page shows the same tables, published by `aws_page.publish`.
The user renamed it from AWS prices to AWS on 2026-11-15, because it holds spend, prices and quotas.
The published file stays `aws_prices.html`, so older links still open it.
A Spend card at the top shows today, yesterday and the month to date, with the month forecast below them.
`spend.read_spend` and `spend.month_spend` give these numbers; the buildlog report reads the same functions, so both show the same values. Read the spend on the console AWS page; the `/aws_spend` command was removed on 2026-11-16.
The day is the UTC day. The hourly buildlog run publishes the page, so the spend is current to the last hour.
After each push to main, `.claude/config/post_push.sh` publishes it again
when the push changes the page code and the pushing checkout is at the pushed commit, as the promotion step's ~/.claude is.
The Prices and quotas card follows the Spend card.
The user approved the combined table on 2026-11-18.
1. The sticky filter area shows Geography, Regions, Show, Types, and Runs in that order.
   Show starts with Spot price, Cost per gate, and Quota on.
   Each Show chip toggles its columns.
   Types selects Standard or GPU, with Standard on the first view.
2. The first two columns show the region and its quota in vCPUs.
   The quota is Standard quota or G and VT quota for the selected types.
   The quota has a blue-gray tint and stays visible when all Show chips are off.
   The next three columns show the requested vCPUs, request state, and request age.
   A region without a request shows a dash in those columns.
3. Each type header includes its vCPU count.
   When multiple values show, each type groups the Spot (USD/h), Gate (USD), or Fit (boxes) columns.
   Fit divides the region quota by the type's vCPU count and rounds down.
   GPU types omit Gate and show `GPU types have no gate runs.` when only Cost per gate is on.
   Gate estimates use the Spot price times the median box time, plus the refusal wait cost.
   A region with a cached Spot price shows `read failed` or `old` next to its name.
   A ban icon with the title `Not sold here` identifies a type that the region does not sell.
   Unavailable values show a dash with the reason where known.
4. The Runs chips show when Cost per gate is on.
   Merge gates, Targeted runs, and Both select the Gate estimates and the run tables.
   The choice also selects the refused and launched tables.
   The billed hours table does not depend on the run choice.
   The run tables omit Spot price and estimate because the Prices and quotas card shows them.
5. The Spot and Gate columns use separate color scales and marks.
   The scale uses green for the minimum and red for the maximum.
   A median between those limits uses the theme's middle color.
   A white box with a dark edge marks column extremes when the column has more than two values and the extremes differ.
   Gold marks the cheapest value of each measure, and black marks the most expensive value.
   These marks replace the scale and white box for those cells.
   Quota and Fit cells have no scale or marks.
6. One legend, the snapshot lines, and the quota case instructions follow the table.
   A page refresh keeps the horizontal scroll position.

A region filter at the top of the page shows all regions by default; the user asked for it on 2026-11-14 through builds.
1. A chip for each region toggles it. `All` shows every region again.
   The chips show the groups `us`, `eu`, `ap`, `ca` and `sa` in that order, each A to Z; a region outside them comes last.
   `aws_page.region_filter_key` sets the order; the user asked for it on 2026-11-15 through sdlc and builds.
   `None` at the end clears every region, so one click then shows only that region; the user asked for it on 2026-11-15 through builds.
   With no region chosen, each card says `No region chosen.` A reload with no region saved shows every region.
2. Every section follows it: tables with a region column drop the other columns, and tables with a region row drop the other rows.
3. The colors, the box and the table marks use only the chosen regions. `Table.values` holds the number of each cell.
   One region has no column scale or column box.
4. The cheapest line names the cheapest estimate in the chosen regions (`RunCosts.estimates`, cheapest first).
5. A snapshot line for one region shows only when that region is chosen.
6. The page keeps the region choice in browser storage. The URL keeps Show, Types, and Runs as `?show=spot,cost,quota&types=standard&runs=merge-gate`.

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
3. The check counts the first exposure per generation in the configured Hana region and splits its hours at UTC midnight.
   The check counts open exposures through check time and adds the same regional total to the upper bound for each Hana type.
   The check uses the gate and builder hours for the original ratio comparison when the bill is below them or the Hana allowance is zero.
   A positive allowance raises the upper limit to `(box hours + Hana exposure hours) × (1 + limit)`.
   Both comparisons use `billed_hours_gap_max_ratio` from `aws_target.conf`.

Hana exposure hours can hide that many hours from an unknown box of any configured Hana type in the region on that day.

`run_cost.day_hours_checks` gives one `HoursCheck` per type, region and day in the window.
Days run from the window start day up to, but not including, the end day, like the bill split.
`run_cost.hours_checks` sums the days with a bill read for each type and region, for the billed hours table on the AWS page.
`HoursCheck` records `billed_hours`, `box_hours`, `hana_reserved_hours`, `hours_gap_ratio`, and `hours_gap_over`.
The gap ratio is the distance outside the range divided by the bound on that side.
Without a bill read, the recorded and reserved hours count every day in the window.
`aws_target.hourly` checks the last `run_cost_days` days after its price read.
`buildlog hourly` and `aws_target.py snapshot` both call it.
The notices and the table use the same list of gate types and Hana's primary and fallback types.
If the Hana region or type settings are unavailable, both use the gate types and a zero Hana allowance.
For each type, region, and UTC day above the limit, the check sends a message to bob's fixed staff ID.
The message gives the billed hours, ledger hours, gap, and limit.
The message includes exposure hours for the region and day when a Hana allowance applies.
It asks bob to file an investigation and add it to the list.
The check saves the key `aws-hours-gap:<type>:<region>:<day>` in `hours-gap-notices.json` after `SENT`, `SKIPPED`, or `QUEUED`.
After a failure or timeout, the next hourly run tries again.
The check uses the sender's full time budget for the subprocess.
The check uses only its claim file to suppress duplicate messages.
The check keeps the two claims from the old phone route, which bob tracks in investigation I7.
The check removes claims for days before the window.
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
Every launch, refusal, fallback, and finish row in this file includes `gate_job`.
It uses the queue's tagged `StepJob` record with `step`, `unit`, `branch`, and `modules`.
When the job is unknown, the field contains `{"kind":"GateJobNotRecorded"}`.
Readers accept rows with missing or unreadable `gate_job` values.
The controller saves the job with the attempt ID in `current.json`.
The request and every launch history row for the attempt use that saved job.
An attempt uses `GateJobNotRecorded` if its saved job is missing or unreadable.
Resumed launches keep the saved ranking and evidence without another live read.
Placement does not try a type and region whose median box time, boot included, is above `max_gate_minutes=12`.
The user, 2026-11-14: a slow type costs more gate time than its lower price saves.
The median uses the type's runs in that region, or its runs in all regions when the region has none.
A type without runs is tried; `lifetime_minutes` is not a measurement.
The worker ranks alternatives by recent quota refusals, estimated live quota use, and recent zone refusals.
The quota check adds the new box to known live gate vCPUs.
A G or VT type uses the G and VT quota. Other types use the Standard quota.
The check uses quotas from the hourly snapshot within `price_max_age_seconds`.
The worker prints `Cloud gate skips <type> in <region>` for a type above the median time limit.
If that limit removes every variant, the gate falls back to natedev.
Within each group, variants with a current price come first, ranked by the expected USD per run.
The user, 2026-11-14: "base our spot Requests on lowest price per run".
If any variant has a current price, equal costs and unpriced variants follow the `instance_types` order, then input order.
If every variant lacks a current price, the worker preserves input order within each capacity group.
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
The configured request is 136 Standard Spot vCPUs per gate region.
`cloud_gate_slots=16` in `build_admission.conf` limits concurrent cloud gates in the queue.
`slots=9` in `cloud_gate.conf` limits the module count for one paired trial.
The same command requests G and VT Spot quota (`L-3819A6DF`) in each gate region from `regions`.
A region in `spot_candidate_regions` only has no gate stack and gets no quota request.
The GPU quota request uses `gpu_spot_quota_request_vcpus=32` for one GPU box up to 8xlarge.
These settings are in `cloud_gate.conf`.
Both quota codes use one helper for regional requests.
The command uses the encrypted administrator key through the admin executor.
It skips regions with sufficient quota and prints existing open requests instead of creating duplicates.
It prints the region, desired value, and request status without request, case, or account identifiers.
A failed call prints only the AWS error code or Python error class and does not stop other regions.
Any regional failure makes the command exit with status 1.
The external administrator IAM policy also needs `servicequotas:ListRequestedServiceQuotaChangeHistoryByQuota` and `servicequotas:RequestServiceQuotaIncrease`.
Both quota codes use these same three IAM actions.
`deploy.py quota-status` reads both quotas and their request history with the read-only plan identity.
If the key is missing, the command names the plan identity setup command.
If the key is locked, the command tells you to run `github-warmup` in a terminal on natedev.
If AWS rejects the plan key during any call, the command stops and names `deploy.py plan-identity --check`.
It shares the quota codes and region lists with `quota-request`.
Each region starts one terminal block with its name as the heading.
Each quota shows its applied vCPUs and the vCPUs needed from `cloud_gate.conf`.
The newest request shows its status, requested vCPUs, and age.
The command uses creation time to select the newest request and calculate its age.
If a request has no creation time, the command uses its last update time.
A request that has neither date shows `age unknown`.
The report keeps unknown status text and counts the request as open.
A quota with no requests prints `No requests.`
The final summary lists regions whose applied GPU quota fits the trial and requests that remain open.
The summary uses the trial's shared vCPU calculation over `gpu_size_trial_spot_max_usd_per_hour`.
It does not use the requested quota as proof of an applied quota.
When every applied quota is too small, the summary says `no region has enough applied quota`.
An unread applied GPU quota adds `unknown for <regions>` after the regions that fit.
An unread request history makes the summary say `open requests: unknown` and name the unavailable region and quota.
The report omits account IDs, case IDs, request IDs, and ARNs.
Other regional failures print the AWS error code or Python error class and permit the remaining quota reads.
Any regional failure makes the command exit with status 1.
The command uses only `GetServiceQuota` and `ListRequestedServiceQuotaChangeHistoryByQuota`, including every history page.
It makes no quota request, paid API call, or local state write.
The plan identity policy grants both read actions in `ReadSetupInputs`.
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

After deployment, `deploy.py up --workload merge-gates --approve-resources` checks GPU access with the trial scaler profile.
Setup compares the profile's caller ARN with the stack's scaler user ARN. A mismatch fails setup.
It reads instances, offerings, Spot prices, and quota in the home region.
It uses the trial's Cost Explorer reader and probes local credential export.
It sends a tagged `RunInstances` DryRun with the trial launch template when a saved subnet offers the type.
If no saved subnet offers a type, setup reports the launch as unverified for that type.
Setup reports each wait before it retries a denied launch DryRun.
The `gpu_setup_dry_run_retry_timeout_s` and `gpu_setup_dry_run_retry_interval_s` keys in `cloud_gate.conf` set the deadline and interval.
The template supplies the instance profile.
The launch request checks tagging at creation. [EC2 tag permissions](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/supported-iam-actions-tagging.html)
If a matching GPU Spot instance exists, setup checks termination with DryRun.
The administrator simulates the scaler user's termination grant in each saved Spot candidate region with subnets.
Each simulation uses a sample instance ARN, the required tags, and `aws:RequestedRegion`.
A simulated denial fails setup.
If the administrator lacks `iam:SimulatePrincipalPolicy`, setup reports termination as unverified.
Setup writes one fixed probe object under each GPU trial prefix and checks the contents through GET.
Setup lists the exact result key. The trial lists result keys only.
Setup creates and aborts a multipart upload on the input probe key.
A failed required probe makes setup exit with status 1.
Setup prints one result per operation and one advice line per error kind.
Setup directs the user to check the grant for authorization failures or repeat setup for transient failures.
The probe creates no EC2 instance. The pilot still checks capacity and guest boot.

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
| `availability_zone` | `us-east-2c` | Use this zone for the first launch candidate. |
| `ami_id` | Empty | Use the reviewed image from the registration. |
| `ami_owner_id` | Empty | Use the owner from the registration. |
| `vpc_id` | Empty | Use the VPC from the registration. |
| `subnet_id` | Empty | Use the subnet from the registration. |
| `aws_timeout_s` | `10` | Bound small provider and receiver requests. |
| `result_transfer_timeout_s` | `120` | Allow time to transfer up to 32 MiB of result JSON in either direction. |
| `control_url_seconds` | `3600` | Bound signed access for one boot. |
| `control_poll_seconds` | `1` | Read current permission before the watchdog interval ends. |


### Replace the Hana root image

The NixOS configuration builds the guest files from its pinned `claude-commands` input.
Use a reviewed pin that contains the current rendered guest files.

1. Build the image with `nix build /etc/nixos#nixosConfigurations.aws-hana.config.system.build.images.amazon --out-link RESULT`.
2. Register the image with `scripts/aws_target/deploy.py hana-image --image RESULT --approve-resources`.
3. Check the registered image with `scripts/aws_target/deploy.py hana-setup --plan`.
4. Apply the root replacement with `scripts/aws_target/deploy.py hana-setup --approve-resources`.

The plan reports the deployed image freshness before it checks the registration or simulates the policy.
The replacement plan reads AWS through the plan identity and does not write local state.
Setup replaces the root volume on the same instance and keeps the data volume.

Before the first attempt, setup checks that the box is stopped and has no claims.
It checks the lifecycle identity and the retained data attachment.
Setup requires an image registration for an existing box.
The registered guest digest must match the current guest files and the EC2 image description.
A pending attempt requires the same image and guest digest as its saved record.
On each invocation, setup checks the registered image owner, architecture, virtualization, boot mode, billing platform, and product codes.
If the image root exceeds `root_gib`, setup refuses before it makes any change.

Approved setup installs the replacement grants in the existing Hana `ScalerPolicy`.
Setup uses the scaler credentials for EC2 replacement calls.
Start and stop use the existing Hana deployment and workload tag conditions.
Task creation grants access to the exact Hana instance, registered AMI, and source snapshot.
The task and new volume grants cover resources in the Hana account and region.
EC2 requires wildcard resource scope for describe calls.
The new describe grants apply only in the Hana region.
[EC2 IAM actions and resources](https://docs.aws.amazon.com/service-authorization/latest/reference/list_ec2.html)

The replacement record blocks claims, work starts, and permits.
The controller keeps its stop duty when the compute reservation expires.
Setup reserves compute through its time limit before it starts the box or continues on a running box.
It gives the root replacement task a saved client token.
EC2 deletes the previous root when the task completes.
An EC2 stop and start can change an automatically assigned public IPv4 address.
[EC2 root replacement](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/replace-root.html), [EC2 stop and start](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/Stop_Start.html)

If setup stops early, run the approved command again.
Setup sends the saved token again and polls the task ID that EC2 returns.
It waits for an active task and refuses to start an interrupted Spot instance.
A failed task reports its state and the AWS message when one exists.
The next invocation uses a new token after either task failure state.

Setup completes when EC2 reports the registered image and the same data attachment.
It requests a stop, closes the reservation, saves the image and guest digest, and removes the hold.
If the box already uses the registered image, approved setup records its verified digest without replacing the root volume.
The controller completes the stop if EC2 still reports a running box.
If replacement execution exits early, setup requests a stop and leaves the reservation open.
If preflight refuses a rerun, only the controller can request a stop.

## Hana source transport

`hana_transport.SourceSender` captures exact source through `copies.capture_source`.
The copies private index includes staged, unstaged, deleted, and permitted untracked files.
`apply_changes` uses the same private tree capture.
The caller's index and refs remain unchanged.
The existing tree key and a second private capture detect content and executable mode changes.
Both capture paths refuse new Git links with `NestedRepository`.
The sender never sends ignored files. A build can read these files, so the source check refuses each ignored file outside `source_allowed_ignored`.
The refusal names the first path. Remove the file or ask the user to approve a list entry.
The sender refuses credential paths, Git links, and disallowed environment variables. It omits HOME and target output.
The sender checks local input paths before it requests a Hana claim.
The sender refuses the source early only if it confirms that no execution is bound.
Hana can start if an ignored file appears after the first check. The check after adoption then refuses the file.

When source preparation fails before the command starts, offload reports the cause as a typed admission refusal.
The queue records `DoNotRetry` and the build runs locally.
If the runner cannot release the claim, the queue worker releases it after the runner exits and the lifecycle operation ends.

A frozen `SourceManifest` carries the payload digest, signed URL, and runtime ABI.
It also carries the repository key, private commit, basis commits, command, features, environment, and toolchain digest.
Its JSON fits the receiver's 65536-byte control limit.
Each repository retains a bounded list of guest-acknowledged commits.
Each snapshot commit has no parent, so bundles exclude the caller's Git history and deleted credential blobs.
Later bundles exclude those commits and their reachable objects.
A typed `MissingBasis` answer clears that repository's basis before a complete bundle retry.
`retry_complete` retains the captured commit even when the caller changes working files.

`AwsSourceObjects` implements the typed object boundary through the existing provider.
It uses conditional `PutObject` and signed `GetObject` access under `source/<deployment>/`.
The scaler policy grants only `s3:PutObject` and `s3:GetObject` for that deployment's source prefix.
The sender bounds the 1 GiB payload upload with `source_upload_timeout_s` (300 seconds).
Credential export uses `aws_timeout_s` (10 seconds).
The guest receives no AWS credentials.

The bootstrap downloads the payload with curl and verifies its SHA-256.
The image sets the maximum file size and `source_download_timeout_s` (120 seconds).
`hana_source.materialize` verifies Git prerequisites, checks the advertised commit, and fetches verified objects.
The data disk retains one bare mirror for each repository under `/var/lib/hana/mirrors`.
The shared maker allocates mirror metadata and temporary bundle files.
`build_slots.take_slot` supplies the only working checkout and retains its lease through the caller's context.
The slot API provides rescue before reset, stable Cargo output, and existing capacity refusal.
The slot's `rust-toolchain.toml` digest must equal the manifest before execution.

The guest must use host name `aws-hana`.
Admission requires `roots.slots.aws-hana`, `slots.aws-hana.hana.max`, and `slots.aws-hana.hana.max_bytes`.
The image supplies these approved values and includes Git, curl, and Python.
The per-run runtime supplies the source, maker, slot, rescue, and configuration modules.
The runner retains inherited slot locks through command execution.

## Hana result transport

The command runs in a transient systemd service with `ExitType=main`.
The runner detects Cargo exit even when a descendant keeps an output pipe open.
When the runner observes Cargo exit, it measures the bytes pending on each pipe.
It reads those bytes within the transport size limit, closes both pipes, and returns the Cargo status.
The payload exits after the runner returns and the payload cleans up its temporary files.
When the payload exits, systemd stops the service and kills remaining processes with `KillMode=control-group`.
The receiver requires launcher exit, an empty command cgroup, and a free maker lease before it uploads the result.
It also requires this evidence before it confirms a fence for a running command.
The execution journal stores the systemd exit time, first free lease probe, and upload start under `observation.timing`.
The receiver also records when it first confirms unit exit.
After a restart, the receiver can read the journal without the original launcher object.
The receiver retries a missing journal entry on each pass until it records the exit time or confirms departure.
The systemd exit time is absent if the guest cannot read the matching journal entry.
Each time uses Unix seconds on the guest.

The guest sends `result.json` with both compiler output streams and the command status.
A unit timeout takes precedence over a complete result because cleanup can still be in progress.
The JSON limit is 32 MiB.
The guest and sender use `result_transfer_timeout_s` for the upload and download.
The guest uses curl for the upload and keeps the signed URL out of process arguments.
The receiver starts curl as a child process with `--max-time`.
It checks the child once per pass while it continues to read permits and fences.
The controller starts permit renewal with 105 seconds left under the default settings.
The separate watchdog can stop the guest before a 120-second upload ends.
The receiver reads and processes the fence before the permit in every pass.
A control step can fail during a read, a write, decoding, or validation.
The receiver continues to process the other control requests in the same pass.
A fence sends SIGKILL to the upload process and reaps it before the receiver acknowledges the fence.
The fence observation includes the upload start time when the receiver has recorded it.
During settlement, the controller saves the fence observation when it carries timing.
The receiver records an upload attempt before the PUT and records its result before it publishes the observation.
After a restart, the receiver records an unconfirmed upload as `HanaResultUploadFailed` and reports the loss.
Command and fence documents and execution observations have a 64 KiB protocol limit.
They use `aws_timeout_s` (10 seconds).
The guest publishes the terminal observation after the result upload completes or the receiver records a loss.
The guest checks the expiry of the boot URLs and the attempt URLs before starting an upload.
The sender reads the boot expiry from the saved bootstrap record.
The guest and sender reserve 161 seconds under the default settings for fence settlement.
The sender polls for the exact fence acknowledgement at the control poll interval.
It polls after every fence PUT attempt, including when S3 returns an error status.
The sender retains the claim if the window ends before it confirms the fence, command termination, and lease release.
The offload reports a loss if the transfer cannot fit, times out, or the guest cannot confirm its PUT.
The offload requires an accepted result before it reports a build verdict.
The offload publishes the result and saves its exit status before it attempts to report completion.
If a record, admission report, or diagnostic fails, the offload keeps the published verdict and returns its exit status.
An interrupt during completion reporting can change the exit status, so the caller rejects the result and repeats the run.

## Hana runtime per run

The sender packages its runner files and the full or delta Git bundle in one uncompressed archive.
`scripts/aws_target/hana_render.files` lists the Python files for the run.
The archive also holds the runner entry point, resolved config, and the runtime ABI marker.
The existing conditional PUT sends the archive once.
The signed GET and SHA-256 cover the entire archive.
Complete-source repair reuses the captured runtime with a full Git bundle.

The receiver checks the declared ABI before it starts the unit.
A mismatch reports both versions through `HanaCommandAdmissionRefused`.
Compatibility with versions that predate the ABI depends on the image freshness guard before command publication.
Automatic placement can continue locally after settlement.
A pinned target reports the refusal.

The image bootstrap verifies the payload and its ABI inside the unprivileged command unit.
It gives the runner the verified local Git bundle.
The runner reads the per-run shell, disk floor, Git timeout, environment rules, and maker settings.
The bootstrap applies the image's approved roots, slot capacity, and probe timing.
The runner uses the image's command timeout.
The runner sends the reason for an expected refusal.
If an exception escapes the runner, the entry point sends a refusal that names the exception class.
It flushes the reply before it tries to print the traceback on the box.
Systemd removes the per-run runtime filesystem when the unit ends.

## Hana placement adapter

Hana retains the configured SSH target family and uses signed control instead of an SSH connection.
`target_config.read_execution_transport()` selects the transport before any connection settings or provider probes.
`hana_placement.py` exports the shared transport, generation, claim, and waiter types.
`hana_placement_adapter.py` implements configuration policy, lifecycle evidence, exact claims, and release.
`hana_generation.py` keeps the canonical boot identity safe for guest imports.

The active `hana-aws` target accepts one assigned step at a time.
The route accepts check and lint judgments for the normalized Hana repository.
These judgments run direct `cargo check` or `cargo clippy` commands.
The command policy rejects repairs, builds, nextest, final steps, path overrides, and unknown wrappers.
`enabled=on` enables the route in `hana_aws.conf`.
The queue checks the Hana image and capacity before it assigns work.
A pinned request uses only its requested target.

Provider evidence reads lifecycle and execution records through the existing background worker.
The adapter checks the spending preview only for an idle, stopped box.
A ready box with a verified boot generation can pass placement without a spending preview.
The admission process shares one stopped-box preview across placement probes.
The preview expires after `preview_max_age_s` seconds, which defaults to two.
Changes to the ledger, launch history, lifecycle journal, bills, or spending configuration invalidate the preview.
The cache skips previews that use configuration recovered from Git.
A full preview uses the account ledger lock and reads the launch history once.
The preview gives placement evidence only.
For a stopped box, the controller still performs its full reservation and start checks.
Guest command admission requires a valid permit.
The controller alone owns demand starts, spending reservations, and idle-stop serialization.
One exact lifecycle claim excludes all other Hana worktrees.
Boot generations include the deployment, demand-start token, and verified boot identity.
Permit renewals retain that generation.

F1 receives `HanaClaimPending`, `HanaClaimAdopted`, `HanaClaimRefused`, `HanaClaimRetained`, or `HanaClaimResourcesHeld` from `claim_assigned()`.
`HanaClaimResourcesHeld` means a resource command holds Hana and prevents a new claim.
Only an adopted claim can request the queue's first launch authorization.
At authorization, the queue reads the exact Hana claim and lifecycle row from local files.
These reads take no provider locks and make no AWS calls.
The queue checks the claim, holder, session, host, boot generation, and active permit.
The queue also checks the estimated memory peak against the Hana memory limit.
Before publication, the sender reports a source, holder, authorization, permit, or journal refusal with its cause.
If the queue refuses authorization, the sender uses the queue's reason.
The queue reports whether adoption evidence is stale or unavailable and includes the reason.
If release fails, the sender reports pending settlement and the queue keeps the assignment.
The runner returns the confirmed guest refusal with its original reason and records that cleanup remains unconfirmed.
The runner also returns an accepted command result with its output and exit status while claim release remains pending.
`release_assigned()` requires F1's durable command settlement before lifecycle and target claim release.
An exact guest observation can settle an accepted execution that ended without a compiler result.
The observation must confirm termination and lease release, and the sender must have issued no source update.
The sender uses the guest cause when it is available.
If the guest requests a source basis or reports a pending result, the sender uses its own cause.
For other confirmed departures, the sender attempts to release the claim without another fence observation.
The sender requires a fence acknowledgement after a result transfer loss or when it resumes an unsettled fence.
The sender uses the refusal code to report `HanaCommandAdmissionRefused`.
An unsupported runtime ABI uses this same refusal and records `DoNotRetry`.
The refusal proves the command did not start.
The sender reports the refusal before claim cleanup.
The controller checks whether the holder exited in every lifecycle state, including after a Spot interruption.
An unresolved start still requires provider recovery.
The next Hana evidence read can remove the detached target claim.
The admission service checks saved current assignments on each queue pass, including after a restart.
For a dead local holder, the Hana evidence worker can replay exact terminal proof from the dispatch journal.
The journal, guest observation, and retained claim must identify the same boot.
The worker uses the normal settlement path to release the claim and record settlement.
Recovery preserves a saved pending report's outcome and sends that report as settled after claim release.
The sender checks the current source when it accepts a result.
If fencing or claim release remains pending, the sender reports the accepted completion with `DispatchPathPending`.
Recovery uses the result decision from the journal.
The queue consumes the settled execution report on its loop thread and closes the assignment.
If the service restarts after claim release, it can read the settled journal and finish the report.
If the journal confirms command settlement and the claim is gone, recovery completes any unfinished journal write and sends the report.
Missing proof or proof for a different dispatch keeps the assignment current.
The queue excludes Hana for the refused build and continues in the configured candidate order.
For a different build, the queue needs provider evidence at or after the refusal report before it can assign the same scope.
For a pinned build, `verify.sh` prints one line with the refusal cause and exits with status 2.
An ended local holder cannot settle an unresolved Hana dispatch.
A failed boot needs a terminal `ExecutionReport`; pending settlement retains capacity and the live retry deadline.
The queue permits fallback only after settlement.

The queue applies worker evidence on its loop thread and invalidates stale read snapshots.
An exact current assignment and live holder establish the `remote_waiter` hold classification.
A drain preserves that waiter without granting local memory or a Cargo token.
Unassigned callers retain the existing drain behavior.
Phase E adds no stored shape or queue wire field.

### Pinned Hana GPU nextest pilot

Run the gate setup command on natedev.

```sh
python3 scripts/aws_target/deploy.py up --workload merge-gates --approve-resources
```

Approved gate setup removes the old `gate-probe.json` before it can change gate resources.
An interruption or early setup failure leaves the record absent.
When the probe ends, it saves the verdicts in a private record beside `gate-deployment.json`.
The record includes the deployment ID, the UTC end time, and any failed checks.
Run `deploy.py probe-status` to read the saved verdicts and record age without contacting AWS.
The command shows verdicts only for a readable record that matches the current deployment.
It checks the recorded regions and instance types against `gate-deployment.json`.

After the GPU setup probe passes, set `hana_gpu_route_enabled=on` in `config/cloud_gate.conf`.
The route checks the deployment record for every type in `hana_gpu_instance_types`.

Run the pilot from the Hana checkout.

```sh
~/.claude/scripts/direct/verify.sh test hana_mimesis_tools --test shader_variants --target hana-gpu
```

The queue offers one Linux slot for the package and test pairs in `hana_gpu_selections`.
`verify.sh test --target hana-gpu` omits the implicit workspace dev features.
The executor captures the caller tree and passes the commit to the one-step GPU nextest worker.
The worker uses the selected nextest profile and returns the exit status and both output streams.
Cancellation in `verify.sh` stops the assigned GPU service and starts its cleanup.
If only the runner dies, the GPU service can continue until the trial expires.
The guest shuts down by the booking start time plus `hana_gpu_lifetime_minutes` from `config/cloud_gate.conf`.
The gate expiry job uses the same deadline and the reservation allows 67 additional minutes for delivery and retries.
The queue retains the claim until cleanup confirms termination or the ledger confirms that all bookings for the trial are closed.
A settled infrastructure refusal releases the slot.
A `HanaCommandAdmissionRefused` result prevents another attempt on the target for the same placement request.
The queue gets a settled refusal only after the exact claim is free.
A pinned call prints the refusal reason and exits with status 2.

The `hana-aws` target handles check and clippy steps.

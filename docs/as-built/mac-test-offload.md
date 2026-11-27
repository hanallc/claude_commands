# Mac step offload

## What it is

`verify.sh test` tries the first free eligible build target: the Mac first; a Mac pass or failure ends the call. It waits when every eligible target is held or full. The Mac takes one claim per repository: a hana run and a cargo-liner run can share it, two runs of one repository never do. A block is the Mac's hold: it admits its holder session's own claims and refuses other sessions' new claims for every repository, turns off hana's CI use of the Mac, names skipped macOS runs on release, and sends its holder one free message when existing work ends. Expiry attempts to remove the block; a failed CI restore leaves it in force. A claim whose Mac has run nothing for 15 minutes is released and its holder told. Each offloaded run removes the mirror's build output afterwards only when the Mac is below its free-space floor. `/build:page` prints the shared build page link. The `buildlog` CLI retains coordination state, hold intervals, and detailed Mac step tables.

Converted verify steps execute only at the destination assigned by the queue.
The queue preserves configured target order.
`step_routing_rule` supplies fixed pins, platform constraints, tree-write rules, local artifact rules, and final-step rules.
Tree-writing steps remain local.
Unpinned build and final steps retain their local rules.
A pinned final keeps its clean-tree check and saves no local pass.
A remote build prints its target and binary path.
`linux_only.<repo>` keeps tests, builds, examples and finals off the Mac.
`check`, `lint` and `doc` steps ignore it for every selection: default, `--mac`, `--target mac` and a pinned Mac.
These steps only compile, and only the Mac compiles macOS code, such as the macOS files of `hana_video`.
`eligibility.COMPILE_ONLY_STEP_KINDS` and `eligibility.linux_only_applies` hold this rule.
A step with a named package checks only that package against `linux_only.<repo>`.
A package that depends on a `linux_only` package still runs on the Mac.
A step with no package refuses Mac offload when any workspace member is `linux_only`.

`fixed_placement_constraints` retains every configured candidate that a fixed rule permits.
Disk, busy, spending, reachability, and hold results remain refreshed evidence.
A saved disk exclusion cannot remove a candidate permanently.
The same episode can receive a Mac assignment after disk evidence recovers.
The live step record keeps diagnostic disk evidence.
Standalone fixed local callers retain their existing local admission path.

The queue checks waiters in order for each remote place.
For Mac places only, a lower step's newer free reading clears a higher step's busy result for the same destination.
The higher step can take the place in that pass.
The queue keeps each step's other eligibility results.
A step that needs evidence can hold the place for one waiting pass interval plus the probe time limit.
The remote claim checks the place again before the step starts.

## Mac Linux gate type checks

A gate that runs in the Mac Linux guest runs its requested type checks and tests in the same numbered copy.
The guest puts the type-check environment in the copy's gate tool folder.
For a run with type checks, the guest checks for `basedpyright` and `uv` before it starts the gate.
If a tool is missing, the guest stops the run before a test starts.
The guest returns the type-check result with the test results.
The guest runner writes the `mac-linux run start` line to standard error before the gate starts its suites.
The caller reads guest standard error while the run is active and keeps all its bytes for the result.
The start line for the current guest attempt triggers one execution report for its queue assignment.
The caller limits the configuration wait to the SSH connection timeout.
The report uses the queue query timeout from the time the caller reads the start line.
The caller waits for the report only until its deadline.
After a stream failure, the caller stops SSH before it collects the report.
A report failure or timeout prints one warning and leaves the guest run active.
If the placement caller supplies `BUILDLOG_PLACEMENT_SIDECAR`, it names the placement request's progress sidecar.
The caller removes the sidecar variable from the guest SSH environment.
The guest sends runner records on the reply descriptor.
The route publishes each change to the module count.
The count excludes type checks.
Older guest gate code can return its result without sending progress records.
If a guest attempt fails without a confirmed start, the gate exits with status 79 and reports the exact assignment.
If the gate consumed permission and sent no arm command, it saves proof that the guest did not start.
If the permission reply is lost, the gate checks for saved consumption with the exact launch claim.
The queue checks that the holder ended and that the attempt has no recorded start.
For a placed gate, `copies.py run` keeps the copy lease and submits the original placement request again.
The queue keeps the original rank and assigns the next available candidate other than the guest.
The queue saves the retry decision and restores it after a restart.
The old guest attempt holds its slot until the queue reads exact end proof and checks that no start remains pending.
If the claim failure is missing or invalid, `copies.py run` reports `Gate placement blocked` and returns status 79.
If a confirmed guest start has no result, the gate exits with status 80 and requests settlement.
If the gate loses the permission reply and cannot confirm consumption, it exits with status 80.
After consumption, a failure to release the launch holder or save the refusal also returns status 80.
A Mac Linux gate with status 80 requests settlement without retrying.
The queue keeps the guest slot until settlement and ends the attempt without a retry.
A stream failure reports its status and up to 1200 characters from standard error.
A stream timeout reports the diagnostics it received before the deadline.
A reply that the route cannot read also names its first 200 characters.
The caller must submit a new run to retry a gate that failed after a test started.

The probe, arm, gate stream, and cleanup retry an inner SSH connect reset or refusal.
The Mac gives the inner SSH client a private log and deletes it when the shell exits.
When SSH exits 255 and the log contains only the connect error, the shell reports the error with a reserved status.
The route checks the reserved status, the Mac marker, and empty standard output before it retries.
The gate stream also retries status 0 with the exact connect diagnostic and no reply data.
The probe, arm, and gate stream allow three attempts within the unreachable wait, with one second between attempts.
The arm covers the gate stream's connect window and allows time for the arm reply and guest lock setup.
The gate stream sends the remaining connect time to the Mac after the outer SSH hop starts the Mac shell.
The Mac applies that time to the inner SSH hop. The sender declines the inner hop when less than one second remains.
The local stream enforces the deadline until the guest accepts the command.
The guest acceptance line starts the transfer timeout and the bundle upload.
If the deadline expires before acceptance, the queue uses the retry rule for an unconfirmed start.
Cleanup continues until it gets end proof or the proof deadline expires.
It waits one second between the first three connect attempts, then uses the normal proof interval before a new group.
An interrupt preserves the proof deadline and the count of attempts in the current group.
The hand-made command stream combines output and diagnostics and uses one stream attempt.

## Mac queue runs

`mac_queue_run.py` checks the Mac block for each kind (`shot`, `install`, `probe` and `run`) before placement, before remote preparation, and before the remote command.
Each kind uses the holding unit's session for the block check, so workers of that unit can start a run during its block.
A `run` also uses that session for the placement request; a `shot`, `install` or `probe` uses its own session (`CODEX_THREAD_ID` or `CLAUDE_CODE_SESSION_ID`).
A run that waits for another session's block prints one line with the holder and reason.
A run whose block record cannot be read waits, and prints one line with the record path.
The run returns any placement before it waits for the block to clear.
The wait can lift an expired block through the Mac block expiry procedure.
A failed CI restore keeps the block in place.
`--no-wait` reports the block or the unreadable record and exits with status 78.
`mac_run.sh` exits with status 9 for a foreign block or unreadable coordination state.
It also exits with status 9 when the queue reports that the Mac is busy or blocked.
It retries other placement waits, including waits for host evidence, queue position, or memory.
It prints the claim refusal or queue diagnostic and returns any placement before exit.
Other queue runs retry a busy reply unless they use `--no-wait`.

Doc uses a snapshot of the staged tree.
After success, verify writes a doc pass record if the staged tree still matches the snapshot.
The Mac runner uses the original repository identity and copies the snapshot from `--source-root`.
Default routing excludes the Mac for `doc` from the `.claude` repository through `offload_exclude..claude=doc`.
An explicit Mac pin permits doc for that repository.

Lint judgments, check, and doc use the same availability checks and target reservations as tests.
A free eligible target permits immediate routing, including during a natedev Cargo token wait.
An idle alert marker does not control admission.
Blocks, live repository claims, and retry delays still apply.
Tree repair steps remain local.

Lint during a hold keeps local `cargo fmt` and `taplo fmt`. It then runs `mend-check` (`mend --fail-on-warn`) and clippy judgment (`--workspace --all-targets --all-features -- -D warnings`) on the Mac. A lint can use this sequence when its first `mend --fix` waits for memory and a Mac becomes free. A clean judgment ends the call without local repairs. A busy Mac keeps the lint on natedev. Local formatting uses the protected `local_noncompiling` record state, including when a hold starts during formatting.

A `mend-check` finding starts the Mac fix pass in the same Mac run. The `mend-fix` verb runs `mend --fix --fail-on-warn` on the Mac copy. clippy then judges the fixed copy with `--all-targets --all-features`. On a pass, `return_fix` lists the mirror files that differ from the worktree in content, under the run's claim. It skips the mirror mark, `/.git`, `/target` and git-ignored paths. It writes the changed files and removes the deleted files only when the worktree tree key still equals the sent key. The return is all or nothing, with a roll-forward record in the worktree git folder (`mac-fix.json`, held under `mac-fix.lock`). `fix_return.py` writes each fixed file to a temporary sibling under a `staging` record. It then writes a `commit` record that lists the renames and removals, and fsyncs it. It runs the renames and removals and then deletes the record. At its start, `verify.sh` finishes a left record before any step reads the tree. A `staging` record is rolled back, so the tree is unchanged. A `commit` record is rolled forward. So a stop or SIGKILL at any point leaves the old tree or the fixed tree after the next run. natedev then runs `cargo fmt` and records the verdict key on the fixed tree. The result file adds `fix=applied|none|refused|failed|not_run`. A failed fix pass, a refused or failed return, or any decline other than busy or blocked returns the lint to the natedev queue for `mend --fix`, as before. Other writing steps always run on natedev, and their leave hooks remain disabled while the commands run. A Linux-only lint error can appear later at the gate. A local step stopped for a new hold is routed again after the stop, rather than counted as a completed failure.

## Mac Linux guest folders

The Mac Linux route uses `mac_run.held` to claim a repository on the Mac.
A Mac block or an active claim for the repository can refuse the claim.
The route returns `GuestNotStarted` with the refusal reason before any upload or retry backoff.
The route releases the claim when the guest proves the run ended.
The current API also releases the claim at `unreachable_wait_seconds` (`build_targets.conf`) if the guest cannot prove the run ended.
The guest run lock still refuses another guest run until the first run ends.
When the proof wait expires, the gate returns status 80 after it records the holder's release.
The default root is `~/.local/state/build-targets`. The `BUILD_TARGETS_STATE_DIR` setting can supply a different root.
The admission service creates each missing directory at startup and on reload.
It syncs each directory entry from the state root up to the home directory, or to the filesystem root for paths outside home.
The service logs the result before it accepts placement requests.
If setup fails, the service retries on the next guest placement request.
Until setup succeeds, the service refuses guest placement and launch permission. Other targets remain available.
The gate refuses the launch before it consumes permission if the state root is missing or is a file.
Run `./scripts/lib/py scripts/build_targets/build_targets.py setup-mac-linux` on natedev to perform the same setup manually.
The route records the guest address, machine ID, checkout path, and gate process identity before it requests permission.
The checkout probe accepts one identity line with an LF or CRLF ending.
A malformed reply with exit code zero gets one immediate retry.
For rejected replies, the probe reports the natedev checkout, each exit code, and bounded representations of stdout and stderr.
The arm checks the saved guest identity before it writes any guest records.
It appends records to `mac-linux/end-proofs.jsonl` under the build-targets state directory.
It records `proven` when cleanup confirms the end of a run.
It records `not started` if the gate consumed permission and sent no arm request.
It records `unproven` and saves the armed guest identity when the proof wait expires.
The route sends a completion report when it receives a start marker and a valid result.
When the stream loses the result, end proof leaves the result unknown.
For a proven no-run, it sends a refusal for the consumed claim and releases the holder in the journal.
If the consume or arm reply is lost, the route releases the holder in the journal.
The gate exits 80 if the guest can have run and the gate lacks a result.
The gate also exits 80 if the journal cannot record the holder's release.
The placement loop requests settlement for the exact attempt.
The loop retries an exit 80 in the same episode only if the gate records a refusal for that attempt.
The checkpoint stops when the placement loop returns exit 80.
The queue can acknowledge the end report while it waits to read the proof.
Each record contains the queue attempt key, the result, the detail, and the wall time.
Each record includes the Mac claim ID.
The writer locks the file during the append. It syncs the journal file, the journal directory, and the state root.
A failed report send leaves the proof available on disk.
`mac_linux.settlement()` reads the file without a lock and skips damaged rows.
The reader uses `execution_identity.attempt_key()` with the episode and the attempt as 32 lowercase hexadecimal digits.
A matching `proven` or `not started` row settles the attempt after the holder exits or gives up the attempt.
The queue can call `reconcile_unproven()` while it holds the slot.
If a launch remains unproven, recovery waits for the gate process to exit or give up the attempt.
The adapter reads the journal again before it submits the service.
When the route saved an armed guest identity, the service uses it to confirm the run ended.
For a launch with an unknown arm result, the service requires the saved guest identity.
A guest with a held run lock leaves the attempt pending.
For a known launch, the guest checks both arm names and records refusal under the run lock.
The refusal prevents later arms and runs for that queue attempt.
The service records `proven` if the guest found an arm and `not started` if it found none.
An unrecorded legacy attempt settles after the launch holder and the submitting owner both exit.
The queue uses this rule even if it received a start report.
The recovery path records at most one warning per attempt under `mac-linux/end-proof-warnings`.

natedev holds the SSH input open for the whole run.
When the natedev process ends by any signal, the guest sees its input end and kills the run group, which frees the run lock.
Item 191 B3 will keep the Mac claim until proof and bind the claim identity to the guest attempt.

The Mac Linux route records the HEAD commit of the natedev runner's Git tree.
It checks the index and worktree under `scripts/` against that commit.
It refuses the run for changes, untracked files, or entries with the assume-unchanged or skip-worktree flag.
The full and delta bundles include the runner commit.
The bootstrap tar carries `request.json` with the runner commit.
The guest extracts the runner's scripts and default configuration from that commit.
Both guest entry points use the extracted Python shim and scripts.
The guest main still resets to the candidate's `origin/main`.
The gate runs the selected modules in the numbered copy of the candidate.
The guest removes the bootstrap folder when the shell exits.
After a forced stop, cleanup removes the folder under the run lock before it reports the end of the run.
The reply descriptor is a copy of the stream standard output.
The runner starts the sandbox, and `dbus-run-session` if the guest has it, for the gate run with standard output on standard error.
Only the JSON reply line goes to the reply descriptor, so other output cannot come before it.
It makes a per-run tool folder under the copy's `target` directory.
Bubblewrap binds the tool folder's `tmp` directory to `/tmp` and mounts a private tmpfs at `/dev/shm`.
The guest gate puts suite folders in the numbered copy's `target/test-runs`.
It resolves the build root from the guest account's home.
The guest removes the tool folder when the suite returns.
A forced stop can leave the tool folder in the numbered copy.
On a later allocation, the shared build folder allocator removes the folder if its lease owner has ended and the locks permit removal.
The suite uses a private PID namespace and mounts its own `/proc`.
At the guest deadline, the namespace ends all suite processes, including processes that change groups.
If `bwrap` is missing, the route declines before it sends the source.

## How it works

### State, lock and block — `scripts/mac_test/mac_test.py`

- An `argparse` CLI (`claim`, `release`, `free-now`, `block`, `unblock`, `status`, `watch`, `watch-claim`, `off-on-purpose`) over a state directory, `MAC_TEST_STATE_DIR` (default `~/.local/state/mac-test/`). Installed scripts refuse to run with `MAC_TEST_STATE_DIR` set; tests set it for a checkout's scripts. Records are written to a temp name, then `os.replace`. `state_paths()` names the files; `read_block(path) -> NoMacBlock | PendingMacBlock | ActiveMacBlock` decodes `block.json` and raises `ValueError` on a file it cannot decode. Both are importable with no side effects.
- Four locks: `state.lock` covers `claim`, `release` and each claim watcher read and write, and never waits on `gh`, ssh or a message; `control.lock` covers `block`, `unblock`, `status` and each block watcher pass; `watch.lock` keeps one block watcher (a `systemd-run --user --collect` unit named `mac-test-watch-<pid>`) at a time; `claim_watch.lock` keeps one claim watcher (`mac-test-claim-watch-<pid>`) at a time.
- **Claim files and scopes.** `claims/<repo>.json` holds one repository's claim; `run.json` holds a claim whose worktree is in no repository. `claim_scope(worktree)` names the repository through `offload.repository_identity` (`sweep.repo_name`, the folder holding the shared repository directory), else `WHOLE_MAC`: `ClaimScope = RepositoryScope(name) | WholeMacScope`, and `claim_path(paths, scope)` maps it to its file. `scopes_overlap` is true when either side is the whole Mac or both name the same repository. `read_claims(paths) -> tuple[HeldClaim(scope, path, record), ...]` reads every file, oldest `since` first; a `claims/<repo>.json` scope comes from its file name, a `run.json` scope from its worktree, so a machine-wide `run.json` from an older install covers the repository its worktree is in, or every repository when that worktree is in none. A file it cannot read raises `ClaimStateUnreadable(path)`, a `StateReadError`. `live_claims` removes each claim whose owner ended and returns the rest. A cleanup releases a claim only through `claim_state.cleanup_scope_matches`: a `run.json` claim with a relative worktree and no `repo_name` keeps its whole-Mac scope for admission but matches no cleanup. When the owner of such a kept claim ends, no tool releases it; a person releases it with `mac_test.py release --pid <pid> --claim-id <id> --episode <episode> --attempt <attempt>`, with the values from `run.json`.
Each claim records `claim_id`, `episode`, `attempt`, `submitting_session`, `pid`, `proc_start`, job details, worktree, and queue time.
A mac-linux guest claim id starts with `MAC_LINUX_CLAIM_ID_PREFIX` (`mac-linux-`). `mac_test.claim_kind` reads the kind from this prefix, and the record has no other kind marker.
A remote-bound claim also records `remote_host` and its applicable host generation.
A process-bound claim ends when its checked owner ends.
A remote-bound claim remains held until provider cleanup confirms departure.
Adoption preserves exact claim identity under the target lock.
It can transfer the holder from verify to its offload child.
A cleanup takeover updates the current holder and session but preserves original job evidence.
It grants no new execution permission.
Release compares the exact claim identity.
A PID alone cannot release another attempt.

- `free-now --settings FILE [--repo-root DIR]` reads local records without a lock or remote command.
  It answers `off`, `backoff`, `blocked`, `state`, `busy`, `bad` or `free`, in that order.
  It exits 0 only for `free`.
  A live claim blocks only its repository.
  A kept claim, whose holder process is gone, does not answer `busy`: the next claim takes it over and cleans its leftovers.
  A live mirror refusal time also answers `busy` for that repository.
  A failed target probe blocks every repository.
  A version decline blocks only its repository.
  `verify.sh` passes its repository root with `--repo-root`.
- `block --holder KEY --for TEXT [--hours N] [--showrunner NAME]` stores the holder key and the native session from `owning_session.owning_session()`. The build-hold command supplies the fixed staff ID. A block admits its recorded session’s claims; a block without a session refuses every caller. The block uses `block_hours`, with a limit of `block_max_hours`. It warns the holder `block_warn_minutes` before expiry. A repeat block keeps `since`. `unblock --holder KEY` matches only the stored key. Status lines show the current staff name when the key resolves.
- **CI switch.** `block` turns the repository variable `ci_variable` (hana's `MACOS_CI`) off through `gh` and records a `ci` word: `off_by_this_block`, `off_before`, `not_configured`, `still_on`, `off_unconfirmed`. Unblock and expiry turn it back on only for `off_by_this_block` and `off_unconfirmed`, and list per branch the CI runs that skipped macOS meanwhile, with the `gh workflow run` line to rerun them. The report uses one paged REST list and one GraphQL request for the newest 200 runs; missing or partial job data produces a report error. A lift that cannot turn the switch back on keeps the block, messages the holder hourly and keeps trying. Expiry records that failure as `ExpiredBlockLiftFailed(gh_error_line)` on `SettleOutcome.expired_block_lift_failure` (otherwise `NO_EXPIRED_BLOCK_LIFT_FAILURE`), apart from `ci_problem`; while it lasts `status` prints `The block is past its time limit but could not lift: CI's Mac switch could not be turned back on (<gh line>). Run github-warmup, then unblock.`, and `block` names the same `gh` line. A new `ci` word goes into `CiSwitch`, `decoded_ci`, `ci_needs_restore` and `ci_may_still_be_on`.
- **Pending until idle.** A block is pending while any claim is live (it waits on the oldest one's `what`), or while `ci_activity` reads busy: a queued or in-progress run of `ci_workflow` is busy until it lists a job named `ci_job` with status `completed`. A pending block says `a CI run may still use the Mac`; the stored `waiting_on` kind is `ci_job`.
- **The message.** The block sends one free message with key `mac-free-<since>`. A staff holder receives it through the fixed staff ID. A holder outside staff uses the recorded session socket or given key. The message states which work ended and that the block remains active. A send.py exit status of 0 or 1 counts as delivery.
- Every `gh` call is bounded by `gh_timeout_s`; the send and the session lookup by 30 s (`MAC_TEST_MESSAGE_TIMEOUT_S`). An empty `ci_repo` turns every `gh` call off.
- `/build:mac` accepts `block <why>`, `unblock`, `status`, and `audit`.
  `/build:status [--probe]` reads coordination state and checks reach only when requested.
  `mac_test.py status` calls `full_settle`, which can lift expiry, restore CI, and send pending messages.

### The claim watcher (`mac_test.py watch-claim`)

`claim` exit 0 and takeover exit 13 call `start_claim_watcher`.
It starts the `systemd-run --user --collect` unit `mac-test-claim-watch-<pid>` if no watcher holds the lock.
If the start fails, it prints `warning: the Mac claim watcher could not be started`.
The watcher uses `claim_watch.lock` and runs `claim_watch_pass` every `claim_poll_seconds` (60).
It releases the lock when no live claim remains.

- The watcher reads the claims under `state.lock` and releases the lock before SSH.
  It checks activity once per host, using the recorded `remote_host` or the configured host for a process-bound claim.
  It updates or releases only the exact claim it read.
- The startup probe saves the mirror root and the Mac's `IOPlatformUUID` with the exact claim.
  The offload uses the same mirror root for the lease and run.
- For early release, the watcher confirms that the owner ended and takes the lease for the saved mirror root.
  The remote check reads the Mac identity before it opens the fence.
  The watcher requires that identity to match the saved identity.
  The release path opens the exact start fence and takes its lock without waiting.
  Under that lock, the read-only check counts processes by mirror command, working directory, and descriptor 8 on the fence.
  When the count is zero, the release path removes the fence and releases the claim.
  Work in another repository does not delay this release.
  The SSH call, the read-only check, and the held-fence diagnostic use `leftover_timeout_s`.
- A claim without a saved root or identity uses the Mac idle rule.
  An identity mismatch also uses the idle rule.
  A complete zero count with a missing fence uses the idle rule.
  A held fence, a nonzero count, or a failed or partial check keeps the claim.
- `MacActive` clears `idle_since`. The first `MacIdle` records `idle_since`.
  The idle rule permits release after `claim_idle_minutes` (15) minutes with no Mac activity.
  A failed activity check keeps the claim.
- On release, the watcher prints the reason and tells the holder session if the claim names one.
  It uses the message key `mac-claim-idle-<pid>-<since>`.
  The notification includes a claim command with the saved worktree and, for a remote claim, the saved host.
  The release path starts the block watcher when a block exists.
- `mac_activity_script()` runs over SSH with `BatchMode=yes`, `ConnectTimeout=connect_timeout_s`, and a 10 s limit.
  It reports `active=<n>`, `work=<n>`, and `rc=0`.
  It counts mirror processes, `cargo`, `rustc`, `cargo-nextest`, and executables with `/target/` in the path.
  The watcher sends no signals to those processes.

### The idle Mac watch — `scripts/buildlog/mac_idle.py`

The existing `buildlog-sample.timer` runs the idle check on natedev.
Its default interval is 60 seconds.
The check writes `~/.local/state/mac-idle/alert.json` on each pass.
The hub `scripts/buildlog/mac_idle_alert.py` owns the path, record validation, and alert text.
The views read the saved file and local configuration.
Before a view shows a `MAC BLOCKED`, `MAC IDLE` or `MAC UNREACHABLE` line, it reads the live Mac state with `mac_idle.mac_work` (`mac_idle.view_alert_line`).
That read uses the local block, claim, `unreachable_until` and `failed_probe` files.
It uses `/proc` to check the claim holder's process.
It does not look for a CI job on the Mac, so a saved `free` line can show for one sample beside a CI job that just started.
It compares the cause, not the holder, so a block or idle claim that replaces another one of the same cause shows the old holder until the next sample.
The view shows the saved line only when the live state and cause agree with the saved reading.
A saved line that disagrees is stale, and the view shows no Mac condition until the next sample.
The watch message formats the reading that the watch just wrote, so it does not read the live state again.
The views make no SSH or recipient lookup calls.

The check counts live steps that can use a Mac.
It accepts `AnyTarget` records with Mac targets.
The step must have state `waiting_hold` or `waiting_mac`.
The check validates the owner PID and process start time.
It reads admission waiters and excludes permanent local declines.
Saved disk evidence remains diagnostic.
The queue refreshes disk eligibility without changing the submitted candidate set.
It displays `Mac disk exclusion` and names sufficient Mac disk space as the clearing condition.
Other `natedev_only` reasons do not count.

The `[mac_idle]` section of `config/mac_test.conf` supplies these defaults:

| Key | Default | Purpose |
|---|---:|---|
| `alert_seconds` | 180 | Delay before the view alert and showrunner message |
| `push_seconds` | 600 | Not read. The machine watch uses `excluded_notice_seconds.mac` in `build_targets.conf` |
| `stale_seconds` | 300 | Maximum age of a saved reading |
| `repeat_minutes` | 1440 | Minimum repeat window for an outage |
| `send_timeout_s` | 180 | Timeout for a phone send |

The shared configuration reader supplies these values at use.
The idle watch reads the `[mac_idle]` section directly.
`mac_test.read_config` validates idle settings only for macOS targets.
Linux target configuration reads omit that validation.
`CONF_DIR` selects the configuration folder.
`MAC_TEST_CONFIG` can select the idle watch's configuration file.
Missing or invalid override values use the shipped configuration.
A typed configuration error names a key that has no usable value.
Views show that error as `MAC IDLE WATCH`.

`calls_past_wait_limit` tracks each call by call id and process start in `wait_starts.json`.
It keeps the first observed wait start until that call leaves the waiting set.
Only calls past `alert_seconds` contribute to counts, disk details, notifications, and per-call causes.
An unreadable `updated` timestamp counts as past the limit.
A retry can rewrite the step state every 180 seconds, so its mtime cannot measure total wait age.

`admission_waiters` returns `AdmissionWaiters` or `AdmissionQueryFailed`.
A query failure appears in `SavedAlert.admission_error` and stderr while independent checks continue.
A later successful sample removes that error.
Malformed numeric protocol values become `AdmissionUnavailable` at the admission boundary.

The watch sends the builds showrunner one message after `alert_seconds` while eligible steps wait.
It resolves the address with `showrunners.py address builds`.
The message names the block, idle claim, free Mac, disk exclusion, or unreachable Mac.
It lists the waiting steps and their wait evidence.
The views place the same `MAC IDLE` alert first.
`MAC UNREACHABLE` shows the retry deadline while the connection backoff is active.
After the deadline, running Mac CI counts as work when eligible steps wait.
The `failed_probe` mark keeps the alert visible when the watch finds no work.
The line says that the last probe failed.
`MAC IDLE WATCH` identifies unreadable or stale state.
The unreachable state sends the message after `alert_seconds` while eligible steps wait.
The unreadable state sends no message or phone push.

The machine watch, `scripts/buildlog/machine_watch.py`, sends the phone push. See [build-targets.md](build-targets.md).
It reads the saved alert of the same pass.
The unreachable state uses `mac_idle.unreachable` with `need=blocked` after `excluded_notice_seconds.mac` (600 s) while eligible steps wait.
The failed probe mark keeps the same outage across retry deadlines.
A successful probe clears the mark.
A free Mac, an idle claim or a disk exclusion with waiting steps sends no phone push.
`mac_idle` counts a waiting step only after `alert_seconds`.
The machine watch sends one message to bob and the builds showrunner 600 s after it first sees the waiting steps.
A block sends no phone push.
These states keep the message to builds.
A Mac that the user turned off sends no phone push: a Mac block, `offload=off` or `enabled.mac=off`.
`mac_test.machine_off_on_purpose` holds this one rule for every outage notice and takes any target name.
Each target reads `enabled.<machine>` in `build_targets.conf`.
The local target also reads the `/build:hold` holds in force. An unreadable hold state counts as in use.
An SSH target also reads its block and one switch in its settings file: `offload` for a macOS target, `enabled` for another target.
Only an explicit `off` counts. A missing `enabled.<machine>` key, an unusable value or an unreadable record counts as in use.
A missing live settings file or a missing switch key reads the committed file, as the router does, so the committed `offload` value counts.
A block is a lease that a showrunner gives to one session. It does not count when its holder asks.
`mac.locked` in `send.py` asks with its `sender=` value, so the block holder still reaches the user.
For a Mac that is off on purpose, `send.py` prints `SKIPPED: the Mac is off on purpose (<reason>)` and exits 0.
A sent `mac.locked` push also stops when the user turns the Mac off.
For a Mac in use at send time, `send.py` stores the clear check as `mac_test.py off-on-purpose --asking=<sender> || /bin/sh -c '<caller check>'`.
`mac_test.py off-on-purpose [--asking SESSION]` exits 0 when `machine_off_on_purpose("mac")` gives off on purpose, and 1 otherwise.
A `--machine natedev` forward does not change the check: the sending host keeps the caller's check without the off-check.
Only the host that delivers the push asks; a `--machine natedev` forward asks on natedev and reads that skip as confirmed.
The saved alert, the `MAC UNREACHABLE` line and the builds message stay.
When the Mac is in use again and still unreachable, the push goes on the next pass.
`mac_idle.push_retired` is the clear-check rule: the Mac is off on purpose, the saved state is not unreachable, or no step waits.
`mac_idle.py cleared` exits 0 on this rule, and the push stops its repeats.
The machine watch removes its saved `sent` mark only for a Mac that is off on purpose, an outage that ended, or a change of notice kind.
Otherwise one outage sends one phone push, also across a pass with no waiting steps.
A free-Mac reading with waiting steps is a message outage, so a later unreachable reading starts a new push outage.
The machine watch reads the off-on-purpose state once per pass.
The clear check retires the key, so the same `mac-idle-<since>` key pushes again when the Mac is in use again.
The title is "Mac tests stopped".
The action is "Wake the Mac or check its network."
The body gives the elapsed minutes and the number of waiting tests.
It states that builds cannot reach the Mac.
The unreachable state runs no repairs.

Both sends use outage keys and `--repeat-minutes`.
The repeat window covers at least `repeat_minutes` and the current outage age.
Both sends use `--recipient-key` for confirmed delivery tracking.
The showrunner identity is `showrunner:builds`; the phone identity is `user`.
A socket change does not change the showrunner identity.
Existing callers without this option retain builds' pending-claim behavior.

For these logical recipients, `send.py` holds a separate advisory lock across acceptance checks, transport, and confirmation.
It records confirmation only after delivery or a recoverable queue accepts the message.
It never holds the global state lock during transport.
Contention returns `RETRY` with exit 6.
The watch does not mark a retry as delivered.
A process stop before acceptance permits the next pass to retry.
A stop after confirmation causes no second delivery.
A stop between acceptance and confirmation can cause one duplicate.
Delivery takes precedence over loss in that gap.
This policy covers process stops, including SIGTERM, SIGKILL, and session stops.
It does not cover power loss or promise filesystem synchronization.

The Mac idle watch saves `messaged`, and the machine watch saves `sent`, only after accepted delivery.
The unreachable push uses `mac_idle.py cleared --since <since>`.
That check reads only the saved result.
It clears when the saved state is no longer unreachable, the outage changes, or no steps wait.
Missing and stale readings never clear a push.
A later outage gets new marks and keys.

The claim watcher records `work_idle_since` from its existing activity check.
Cargo, rustc, cargo-nextest, and test binaries under `deps/` count as work.
Other executables under `target/` count as apps.
An app can retain a claim while no work runs.
Any activity still clears `idle_since`.
The existing default 15-minute claim release rule remains unchanged.
A failed activity check changes neither idle key.

The full process scan runs only after `alert_seconds` while work waits.
Repeated stale-record diagnostics print once until their evidence changes.
Repairs run only when no block or live claim reserves the Mac.
The watch reads each waiting call from its step record; `verify.sh` registers no call.
A wait with no leave hook gets one `missing_leave_hook` row with action `recorded` and no message.
The watch writes no marker file and repairs no temporary folder.
The watch starts no build step.
Other repair notices go to the builds showrunner.

Buildlog schema 21 keeps one `mac_idle` row per cause per call.
The `.cases` record prevents duplicate rows.
Failed repair messages remain pending.
An `idle_with_waiter` row records the action `message_showrunner`.
`MAC_IDLE_STATE_DIR` selects a private test root.

2026-11-11: Dailies and their Mac block footer were retired.
Use `/build:mac status` to read the Mac block state.

### Step eligibility — `scripts/mac_test/eligibility.py`

This module holds the settings readers, `mac_step_eligibility`, and the readers for backoff, mirror-busy and failed-probe state.
`build_targets.py` asks it first.
It imports `offload.py` only for an eligible step: the probe, the disk floor and the reachability check.
A refused step never imports the runner.
`offload.py` calls these functions through the module, so one test patch covers both modules.

### The offload runner — `scripts/mac_test/offload.py`

Nextest accepts repeated `--env NAME` arguments and one `--fetch FOLDER` argument.
A nextest copy accepts `--env` for its test environment. It refuses `--fetch`.
Other verbs refuse these arguments.
The runner forwards only named variables. Quoted assignments precede the fixed runner assignments, so the fixed assignments take priority.
A fetch folder must have a plain relative path. The runner removes that folder in the mirror before the test starts.
After a reported test result, the runner measures the folder and copies it before releasing the claim.
This also applies after test failure or when no tests match. A lost or declined run copies nothing.
The runner uses `rsync -rlp --safe-links` and publishes the completed copy through an atomic rename.
Files return to `~/.local/state/mac-test/fetched/<call id>/<folder>`. Existing destinations are refused. A failure or signal removes the partial copy.
A fetch result adds `fetch=fetched|missing|too_large|failed|not_run` to the result file.

After a Linux run reports completion, natedev releases the repository claim before it calls `buildlog.sync.sync_target(target)`.
The exchange selects one configured peer and preserves the host paths from `sync.hosts.<target>`.
AWS rows arrive under `~/.local/state/buildlog/aws-build/` on natedev.
The Linux runner uses `BUILDLOG_SYNC=1` to finish the local recorder write before it reports completion.
This setting controls the recorder write; the box starts no connection.

The exchange uses the existing sync lock without waiting for another exchange.
A paused sync or a target without a host mapping causes no exchange.
The exchange uses `sync.connect_timeout_s`, `sync.reach_timeout_s`, and `sync.rsync_timeout_s` from `config/build_targets.conf`.
A failed exchange prints one line and leaves the hourly sync to retry.
The run keeps its result and its measured duration before the exchange.
The Mac keeps its existing hourly sync path.

Tool parity uses one form for the sender, the probe, execution, and refusal messages.
The normalizer strips each version line and joins nonempty lines with one space.
This includes all lines from `cargo nextest --version`.

Linux cleanup reads the real UID from `/proc/<pid>/status` before it reads process ownership evidence.
It excludes processes from other users.
Cleanup excludes processes that started before the recorded run and ancestors of the checking process.
It also excludes direct children of another user, except children of PID 1.
Slot processes keep their user, but an orphan can acquire PID 1 as its parent.
Other same-user processes with unreadable evidence keep the slot busy.

`verify-linux` steps use the repository claim and build budget for admission.
They do not contact natedev's admission service or obey natedev's hold.
The controller still checks spending before AWS admission.

A missing completion marker reports the runner exit status when available.
It does not prove a link failure.
The attempt record keeps the reason.
An interruption marker reports the instance generation.

The runner command has this form:

```sh
offload.py run --repo-root DIR [--package PKG] \
  [--verb nextest|check|clippy|mend-check|mend-fix|doc|build] --call-id ID \
  [--settings FILE] [--last-decline busy|blocked] [--filter-run] \
  [--result FILE] -- <verb arguments>
```

A remote pass exits 0. A permitted local fallback exits 75.
A remote failure returns the step's own status.
The default verb is `nextest`.
Claims use the verb and the package name, or `workspace`.
Remote doc sets `LINT_CONFIG_FORCE=1`.
Workspace verbs use the same manifest-based Linux-only check as eligibility.
With `--target`, the target record gives the settings for both the platform choice and the copy host.
A `--settings` path that resolves to another file is refused before any copy.
Without `--target`, `--settings` selects the configuration.
`--last-decline` suppresses an unchanged retry notice.

`write_result` writes `mac`, `reason`, and `seconds` from the typed run outcome.
A busy result adds `holder`: its known job, `leftovers`, or `unreadable`.
Verify checks the exact launch permission when a routed Mac command gives no usable result.
The check uses the admission query timeout.
Verify exits with status 2 if the queue consumed launch permission or cannot confirm it.
One exception applies: the runner reported a settled refusal before any test started.
Then no test started and no Mac cleanup is open, and Verify treats the result as a runner decline.
The same exit applies when the runner loses the verdict and cannot confirm Mac cleanup.
The local Verify place becomes free when Verify exits.
The remote claim stays available for recovery until the runner confirms cleanup.

- Declines print `mac_test: not running on the Mac (<reason>)`: `off`, `repo`, `linux_only`, `backoff`; claim `blocked`, `busy`, `state`; probe `unreachable` (with backoff), `disk`; then `copy`. The claim gates work; the probe has no process-count, load or power check: a Mac on battery runs tests. An app open on the Mac does not exclude a test.
- The probe reads the hostname and free GiB against `free_floor_gib`, with no power or process check. Coordination records decide whether the Mac is held or busy.
- `copies.remote_rsync` builds the refresh command for the configured mirror root.
  It excludes `/.git`, `/target/`, ignored paths, and the metadata that copies.py owns.
  The mirror's build output survives the refresh.
- The run uses one ssh call through `lint <verb>`.
  `mac_skip.<repo>` applies only to nextest's `-E` filter.
  `LINT_SWEEP_BUDGET_GIB` comes from `mac_budget_gib[.<repo>]`.
  `remote_test_command` sets `MACOSX_DEPLOYMENT_TARGET=14.0` so dependencies build for the supported macOS version.
  Changing that target causes one rebuild.
  It also sets `LINT_SKIP_SWEEP=1`, so `scripts/lint/invoke.sh` `sweep_after_step` returns at once.
  The Mac's steps start no detached sweep that could outlive the run.
  Normal runs on natedev keep their sweep unless the caller sets `LINT_SKIP_SWEEP=1`.
  The runner prints `mac_test: running on the Mac (macOS, host <host>); tree copied in <N> s`.
  Nextest exit 4 becomes `NoTestMatchedOnMac` and runner exit 75.
  The runner prints `mac_test: no test matched on the Mac; running on natedev instead`.
- **Abandoned compiler sessions.** Before a build on a Mac or Linux target, the step removes old abandoned compiler sessions and their locks.
  It removes them only from its own mirror's default build folder.
  `abandoned_session_minutes` sets the minimum age.
  The bounded walk skips `deps`, `build`, `.fingerprint`, and `examples`.
  It preserves recent sessions and finished sessions.
  A Mac step skips this removal when it has an explicit `--target-dir`.
  A Linux step skips this removal when `--target-dir` selects another folder.
  A Linux step checks tool versions before this removal.
  A missing target or a failed removal does not stop the build.
- **Cleanup.** After `echo mac_exit=$?` the remote command runs a cleanup step whatever the tests returned. When the verb arguments name `--target-dir` (or `--target-dir=`), it prints `mac_cleanup=skipped`. Otherwise it reads free GiB with `df -g "$HOME"`: below `free_floor_gib` it runs `rm -rf -- "$HOME"/.local/state/mac-test/mirror/<repo>/target` and prints `mac_cleanup=0` or the `rm` status; at or above the floor, or when `df` gives no number, it prints `mac_cleanup=kept` and the output stays for the next run. That path is the only thing it removes.
- **Nextest output.** The Mac saves both output streams in a private file under `~/.local/state/mac-test/logs`.
  After success, the runner sends the full output and deletes the file.
  After failure, it reports the saved path and sends a tail of `nextest_failure_tail_bytes` bytes (65,536 by default).
  It also sends the last nextest Summary line, up to `nextest_failure_summary_bytes` bytes (1,024 by default).
  The hourly Mac `tmp_sweep.py` job lists closed, owned logs for vetted manual removal after `nextest_failure_log_days` days (seven by default).
  It measures age from the failure time or, for an interrupted run, the last write time.
  On the Mac, read a saved log with `~/.claude/scripts/lib/py ~/.claude/scripts/mac_test/nextest_log.py read PATH`.
  The reader holds a shared lock until the copy ends.
  The sweep checks for open files with `lsof`.
  It takes an exclusive lock before it lists a log.
  If the log writer cannot start nextest, the runner reports a setup failure.
- `stream_mac_run` holds a `mac_exit=<n>` marker with a status from 0 to 255.
  Later ordinary output invalidates the held marker.
  A held marker supplies the command verdict even if SSH ends with an error.
  `parse_cleanup` records a missing, malformed, or out-of-range cleanup marker as `CleanupUnconfirmed`.
  `ReportedMacStatus` holds the verdict and the cleanup state.
- A cleanup timeout preserves the verdict and records unconfirmed cleanup.
  This applies to each routed Mac command and to the cleanup stream after a fetch.
  A timeout retains the claim for recovery.
  If a stream ends without a usable cleanup marker, `claimed_run` checks for leftovers.
  In that case, it releases the claim only after `LeftoversEnded`.
- When launch authorization has no confirmed reply, one deadline covers the exact release.
  The release helper gives SSH the remaining time.
  At the deadline, the runner stops the release helper and its local SSH process as a group.
- SIGINT, SIGTERM, and SIGHUP stop the SSH child.
  The runner checks remote leftovers and releases its claim only after it confirms their end.
  The exit status is 128 plus the signal number.
  A signal during the claim lets that call answer before cleanup.
  A missing `mac_exit=<n>` becomes `LostMacRun`, unless `mirror-use` reports a busy refusal.
  A busy refusal becomes `BusyMacDecline` and writes the repository's mirror refusal time.
  `mirror-use` forwards the start fence descriptor with its mirror lease descriptors.
  A build child can keep the fence locked after the run wrapper exits.
  Other missing completions report the runner's exit status and exit 75 after cleanup.
  `check_leftovers` uses the two selectors from `mirror_selector_script(scope)`.
  The claim watcher uses `ALL_MIRRORS` for the host activity check and the saved mirror for the read-only release check.
  The command selector uses `pgrep` for mirror command paths.
  The working-directory selector uses `lsof` and `awk`.
  The stopped-work check includes processes with the exact start fence file open on descriptor 8 in the leftover count.
  The fence check supplies PIDs only to the count.
  The mirror command and working-directory selectors supply PIDs to TERM and KILL.
  Release and the watcher keep the claim while the fence is held.
  They report each holder's PID and command.
  If the holder check gives no answer, they report that result.
  A `KnownRepository` scope stops both patterns at the repository boundary.
  `extended_regex_literal` escapes the repository name.
  Thus a lost hana run does not stop a cargo-liner run or a run under `mirror/hana_other`.
  Cleanup sends TERM, then KILL, then counts again.
  The cleanup SSH call uses `leftover_timeout_s` from `OffloadConfig`.
  The runner closes descriptor 8 and settles the fence in the command's SSH session.
  Each routed Mac command uses that limit from the time it receives the verdict.
  A separate cleanup stream starts the limit before it starts SSH.
  It returns `LeftoversEnded`, `LeftoversRemain`, or `LeftoverCheckFailed`.
  A failed check or a remaining process keeps the remote claim.

- Every ssh call takes `ssh_options(config)`: `BatchMode=yes`, `ConnectTimeout`, `ServerAliveInterval=15`, and
  `ServerAliveCountMax` = `unreachable_wait_seconds` (`build_targets.conf`) / 15, rounded up. A session with no answer
  for that wait ends, and the routed run ends as lost.

### `verify.sh test` — `scripts/direct/verify.sh`

`test [<package>] --run-ignored only` runs only ignored tests on the selected target.
It accepts repeated `--filter <name>` flags and `--features <list>`, but refuses `--test <name>`.
Omit the package to select named tests across two packages in one call.
The call must match a test. Its filtered result never supplies a full package pass.
Its saved-result key includes `--run-ignored only`.

`test [<package>] --mac "<why>"` runs only on the Mac. The reason is required and appears only in the first routing line.
A busy Mac waits in `waiting_mac`. The wait uses a Bash sleep and the current target reservation checks.
Each poll reads the runner result again. All other declines refuse the call with exit 2 and name the cause.
A retry backoff does not refuse at once. `build_targets.py probe-backoff --target mac` probes the Mac one time.
A successful probe clears failures that predate the probe.
A failed probe refuses the call as unreachable.
For queued steps, the queue uses the normal Mac probe to check recovery during an active backoff.
Each target gets at most one recovery probe per waiting pass interval.
A Mac-only call never runs on natedev, takes a local build token, enters memory admission, or starts a copy watcher.
TERM, INT and HUP end the wait and record the caller's signal.

`--env NAME` and `--fetch <folder>` require `--mac`. These flags can appear anywhere after the verb.
Verify removes these flags before constructing Cargo arguments and call records.
Test verdict keys include each `--env` value. Build keys exclude these values.
Variable names must be valid and exported by the caller. Empty values are allowed.
Verify saves the caller's values before assigning its own variables. A pipe transfers the values to Python without placing them on argv.
The runner refuses `PATH`, `HOME`, `MACOSX_DEPLOYMENT_TARGET`, `RUSTFLAGS` and `RUSTDOCFLAGS` at the verify interface.
It also refuses prefixes `BUILDLOG_`, `LINT_`, `CARGO_`, `RUSTC`, `NEXTEST_`, `DYLD_`, `MAC_TEST_` and `VERIFY_` there.
The fetch folder must be git-ignored and relative to the mirror root. Empty, `.` and `..` path parts are refused.
One trailing slash is removed. Tests must write to that root-relative folder, even when nextest runs them inside a package folder.
Each test uses the terminal values from `verify_test_fixed_env` in `config/direct.conf`.
The defaults are `TERM=xterm-256color`, `COLORTERM=truecolor`, `TERM_PROGRAM=verify`, `COLUMNS=80`, and `LINES=24`.
The rule includes local tests and the test step of `final`.
Test keys hold configured fixed values, instead of the caller's terminal values.

`result_environment.py` reads `verify_result_env_names`, `verify_result_env_prefixes`, and `verify_test_fixed_env` from `config/direct.conf` at use.
The selected name defaults include `NO_COLOR`, `CLICOLOR`, `CLICOLOR_FORCE`, `FORCE_COLOR`, `CI`, `TZ`, `LANG`, and the configured `LC_` categories.
`LC_TERMINAL` and `LC_TERMINAL_VERSION` are excluded.
The prefix defaults are `HANA_`, `WGPU_`, `BEVY_`, `INSTA_`, and `TRACE_`.
These values enter test verdict and tree-content keys.
Unset and empty values differ. These new inputs leave build keys and other command keys unchanged.
A remote nextest run clears each selected name the call did not set, then applies the call's values.
Mac and SSH nextest runs receive these values and `RUST_LOG`, `RUST_BACKTRACE`, and `RUST_LIB_BACKTRACE`.
The same rule applies to unpinned runs, `--mac`, `--target`, and copy runs.
Other remote verbs receive no new values.

A Mac-only call reuses only a Mac pass. Forwarding or fetch disables cache reads and writes.
If tests pass but fetch fails, the call exits 2 and retains the Mac pass evidence.
A test failure retains the runner's status. No forwarded name, value or folder is added to the build log.

A new admission wait clears an inherited queue identity. A rejoining step restores its own identity after sourcing the gate.
An interrupted run stops and reaps both log readers after recording its result. This also handles a blocked output pipe.

- `--local "<why>"` pins execution to natedev and records `declined` / `local_flag` with `BUILDLOG_LOCAL_REASON`.
  The reason must be present, nonblank, and must not start with `--`.
  Reservation words also refuse with exit 2 before work starts.
  The words are reserve, reserved, reservation, hold, holds, held, blocked, claim, claims, busy, showrunner, and ruling.
  The refusal points to `/build:mac block <why>` for a reservation.
  The reason does not enter the tree key or Cargo arguments.
  `compose_nextest_args` constructs nextest arguments for `try_mac_test`.
  `VERIFY_MAC_RUNNER` supplies a runner stand-in in tests.

- `declined` and `lost` allow a local run. No usable result is recorded as `declined` / `runner`. While natedev waits for memory or a hold, each temporary result keeps the Mac leave hook. Routing and local waits share `remember_mac_retry` and `mac_retry_hook`: `busy` and `blocked` use the normal poll; other temporary results, including `lost` and `runner`, set a monotonic deadline 180 s after the attempt ends. Each repeated result sets a new deadline. One stderr line explains the delay per call. A local step that can start proceeds at once. Permanent exclusions (`no_tests`, `off`, `repo`, `linux_only`, `local_flag`) stop Mac retries. A target excluded by configuration is never tried. A `passed` ends the call (`verify.sh: PASS on the Mac (macOS), so nothing ran on natedev.`), exit 0, recorded as `passed_full`, or `passed_filter` on a `--filter` run. Its output is captured and kept as the call's pass record, marked `host=mac` and judged on the tree read just before the Mac run; an edit made meanwhile leaves nothing recorded. A `--local` call does not reuse a `host=mac` record; its natedev pass replaces it. `failed` records the call and exits with the runner's status.
- `verify.sh` and `invoke.sh` create a missing `$TMPDIR` before temp files are made. The lint CLI gets the same setup when it sources `invoke.sh`.
- A signal is forwarded to the runner by `forward_mac_runner_signal()` and waited for; the exit is 128 plus the signal, and the interrupted call writes its last Mac result to the call record. INT goes to the runner as TERM. The handler and `cancel_tree_run` find the runner through `find_mac_runner_pid`, which also sees a runner started before `MAC_RUNNER_PID` is set.
  `find_mac_runner_pid` runs in the calling shell.
  During launch, it uses `$!` only when `MAC_RUNNER_LAUNCHING=1` and that job differs from `MAC_RUNNER_PRIOR_JOB`.
  The handlers therefore cover both sides of `MAC_RUNNER_PID=$!`.
  A subshell would lose this job state.
  Job control around the runner fork would permit SIGTTIN during a terminal read.

  Each remote runner opens its log on descriptor 9 before either tee starts.
  Both tees append through `/dev/fd/9`.
  Removal closes descriptor 9 before unlinking the log, so a late tee cannot recreate the name.
  `record_mac_pass` closes the descriptor before reading, moving, or removing the log.

- Routing reads configured targets through `targets.py`. `verify.sh` waits on held natedev, or tries the Mac while natedev is held; it never falls back to a held target. While both are unavailable it prints `verify.sh: natedev is held by <holder> for <reason>.` and `verify.sh: waiting for the Mac (<reason>) or natedev's release.`, once and again only on change. A local run prints `verify.sh: running on natedev`. Calls carry `mac`, `mac_reason`, `local_reason`, `mac_s`, `mac_holder`, `target` and `hold_wait_s`; `BUILDLOG_MAC_HOLDER` carries the busy holder, is never exported, and is cleared when a later answer is no longer busy. An interrupted retry keeps the earlier decline and holder.

### Mac gate cleanup

An active Mac gate uses the lifecycle script that started the run.
Its close hook stops the attempt with the script uploaded to the Mac during preparation.
Each preparation records its own Mac attempt directory.
A retry uses a fresh directory even if cleanup retains the previous checkout.
When a later queue run finds an ended gate, it calls the installed natedev lifecycle script.
The saved script name identifies the callback.
The current callback invokes the installed Mac gate script to stop the attempt.
It checks the saved checkout path even if the attempt folder is absent.
For older inline checkouts without a journal, it checks for test processes before removal.
Cleanup checks the disk device and mount before it makes one detach attempt per gate image.
A failed detach reports the error and keeps the gate folder.
Custom callbacks use their saved path.
The queue removes its run record and receipt after the callback confirms cleanup.

### The showrunner's Mac build — `scripts/production/mac_run.sh`

After its reach probe, `mac_run.sh` requests a Mac placement for the repository.
It sends a bundle if the Mac source lacks the requested commit.
`copies.py run` holds a numbered copy for the smoke test.
If SSH input closes during the test, the Mac helper stops the smoke process group and checks the copy.

### Which packages belong on the Mac — `scripts/mac_test/audit.py`

`audit.py <repo dir> [--check]` reads workspace manifests with `tomllib` (no cargo) and prints `Package | Gated sites (src) | Gated sites (tests) | Target dependencies | Listed | Suggestion`. It classifies `cfg`, `cfg!` and `cfg_attr` predicates. Linux-only target dependencies mean `keep on natedev`; unclassified predicates mean `needs review`; otherwise `can go to the Mac`. It lists disagreements, review sites and lines not built on the Mac. `--check` exits 1 when an excluded package is missing from `linux_only.<repo>`, 2 for an unknown repository or unreadable manifest. `commands/build/mac.md` also describes process-bound and remote claims for a job of one's own.

### The Mac copy — `scripts/mac_test/copy_watch.py`

A speculative copy remains separate from its local parent attempt.
The parent retains its actual local memory reservation while it compiles.
The watcher submits source facts, parent identity, call start time, and candidate lifetime to the queue.
The queue selects the newest eligible copy by call start time, then call identity.
The watcher requests placement instead of selecting free capacity.
It starts only the assigned attempt.
The offload adapter claims, adopts, and consumes that attempt through the normal provider path.

The watcher preserves first-result handling, loser stopping, logs, fetch behavior, and source locks.
An uncertain loser retains its exact provider claim until departure is confirmed.
A retained cleanup claim cannot yield overlapping capacity.
Changed source content receives receiver write time through `copies.remote_rsync` and `RSYNC_RECEIVER_WRITE_TIME_FLAGS`.
Unchanged content retains its earlier time.

### Build log and report — `scripts/buildlog/`

Call records carry `mac`, `mac_reason`, `local_reason`, `mac_s`, `mac_holder`, `target`, and `hold_wait_s`.
`mac` is `passed_full`, `passed_filter`, `failed`, `lost`, or `declined`.
Readers retain legacy result words for existing records.

| Copy field | Meaning |
| --- | --- |
| `copy` | Last outcome: won, natedev_won, gave_way, declined, or lost. Empty means no copy started. |
| `copy_s` | Sum of seconds that copies held the Mac. |
| `copy_count` | Number of copies started. |
| `copy_natedev_s` | Local nextest seconds from its first Cargo line until the copy decision. |

Verify passes these values through the matching `BUILDLOG_MAC_COPY*` variables to `record.py call`.
Older rows without these fields read as NULL.
Remote steps carry caller `verify-mac` and the controller's `BUILDLOG_CALL_ID`.
`buildlog sync` brings their rows to natedev.
A NULL holder reads `holder not recorded`.
Reports separate known jobs, leftovers, and unreadable holders without changing usable-share calculations.

- `COUNTED_FAILURE` (`status != 0 AND NOT (caller IS 'verify-mac' AND step IS 'nextest' AND status = 4) AND NOT (host IS 'natedev' AND step IS 'nextest' AND call_id IN (SELECT id FROM calls WHERE copy = 'won'))`) drives `step_days.failed`, the `failures` view, the Mac table's Failed, each step table's Failed and the lead block's `Failed steps`. A Mac "no tests to run" keeps raw status 4 without counting as a failure, and a natedev nextest step stopped because its call's Mac copy finished first keeps its raw status (usually 143) without counting either: the stop is the copy's win, not a test result, and the Mac's own step carries the call's real result. The Summary's successes and failures split stays on raw status.
- `report.py`'s step tables, Rebuilds, Memory pressure, Power profile, lead counts and Summary count `host = 'natedev'` through `mac_outcome.natedev_step_predicate(step)`, so callers there carry no host suffix. `mac_section(connection, day)` follows `Tests by target` as `### Mac`: `Caller | Kind | Runs | Failed | Avg | p95 | Range | Build | Tests | Tests failed`, one row per caller and kind over every step with `host <> 'natedev'`, offloaded or started on the Mac directly, busiest first. `verify-mac` reads `verify.sh offload`; other callers keep their `caller_label` text. Failed uses `COUNTED_FAILURE`; Build is the mean `finished_s`; a missing test fact shows `—`. A `Mac total: <n> steps, <time>; <n> failed.` line and a Source line close it; a day with no Mac steps prints `No steps recorded on the Mac.` `report.py` excludes Mac-only calls through `natedev_call_predicate`, and shares the result rules in `mac_outcome.offload_record` with `mac_report.py`. Its lead block gives the Mac usable share and current holds. `tests_by_target_lines` prints `Tests by target`: `Package | Mac ran | Mac passed | Mac failed | Mac p50 | natedev ran | natedev p50 | Did not reach the Mac`. The Waiting table has `Held target`, apart from memory admission. `/build:mac_report` gives windows, decline causes and busy holders, local reasons, usable share and the same-tree failure count.
- `### Mac copies` follows `### Mac`: `Package | Copies | Mac won | natedev won | Gave way | Other | Est. saved | Mac time on copies that did not win`, over the day's test calls with a `copy` word. Copies sums `copy_count`; the outcome columns count calls by their last copy's outcome; Other is `declined`, `lost` and the older `passed_full`; the last column sums `copy_s` of calls whose copy did not win. The lead block ends with `Mac copies: <won> of <started> finished first, about <time> saved (estimated).` A day with no copy prints `Mac copies: none started.` in the section's place and no lead line. A copy-won call counts as Mac ran in `Tests by target` but not as a natedev run (its natedev step was stopped, so its wall time is no natedev run's) and not in `natedev runs avoided`, so a win is counted once, in Mac copies.
- The saving estimate is the conditional remaining time. For a win with `copy_natedev_s` = t, take that day's natedev nextest steps of verify.sh test calls that ran to their end (status below 128, call copy not `won`), of the same command when at least 3 exist, else of the same package; keep those that lasted longer than t; the estimate is the nearest-rank median of duration minus t, or 0 when none lasted longer or t is unknown. A step that lasted longer than t is the only kind that tells how much natedev still had to do once it had already run t; a plain package p50 minus t would count steps that would already have finished. The section's Source lines state the rule and label it an estimate.

### Hold events and tree evidence

A new block writes one `hold_event` `began`; record removal in `expire_block` or `command_unblock` writes `ended`. Repeating a block keeps its `since` and writes no new event. That `since`, including microseconds, is the hold key. The skipped-run query floors its `created` filter to the second so CI entries from the same second are included. Each event is queued under the coordination lock with its UTC `at`; a detached `record.py hold-event` starts after the lock closes. A stalled log cannot delay block or release. Lost events affect report evidence, never the hold itself.

`holds.py` reads current coordination records separately from the `holds` view. Both reports show `Now: Mac blocked by ...`, `Now: natedev held by ...`, a natedev `releasing` line, or state unreadable. The Mac report unions recorded intervals into `Blocked` and `natedev held` for each user's-zone day. `open` requires this machine's matching coordination key and no later row; a releasing holder file still counts until removed. Missing ends read `end not recorded`, bounded by the key's next event when present; missing beginnings read `began not recorded`, affecting every possible day since the key's prior event or target's first event. Unrecorded time adds nothing, and affected days say `at least`. Earlier Mac days use decline lower bounds; earlier natedev days say `not recorded`. Each target closes with its recording start and interval counts, or `holds: no event recorded yet`.

`offload.py` reads `treekey.tree_key` immediately before and after a successful copy. It sends `BUILDLOG_SENT_TREE_KEY` only when both keys are known and equal. `record.step` uses it for a `verify-mac` step whose own keys are unavailable, preserves an own verified key, and never fills a changed tree. A file read raising `OSError` gives no key and leaves the run able to proceed. `Mac failures with a natedev pass of the same tree` counts failed Mac calls with a keyed nextest step and a passing natedev nextest step of the same package, command and key; filtered commands match only the same filter. Steps from before the install still count as `without a tree key`. All windows share one step read, with a 24-hour lookback.

## Config — `config/mac_test.conf`

`fetch_limit_mib` limits one returned folder (default 512 MiB).
`fetch_retention_days` sets the retention period for completed and partial fetch folders (default 14 days).
Both values use the existing configuration reader.

`offload=on`; `host=mac`; `free_floor_gib=100`; `mac_budget_gib=24` and `mac_budget_gib.hana=56` (size limits of each mirror's build folder, not memory); `linux_only.hana=hana_video,hana_clerestory`; `linux_only.cargo-liner=`, `linux_only.bevy_brp=` and `linux_only..claude=` (empty: each key sends that repository to the Mac); `mac_skip.hana=test(world_panel_words_match_drawn_pixels)`; `ci_repo=hanallc/hana`, `ci_variable=MACOS_CI`, `ci_workflow=ci.yml`, `ci_job=macOS: Compile and Test`; `gh_timeout_s`, `block_hours`, `block_max_hours`, `block_warn_minutes`; `claim_idle_minutes=15`, `claim_poll_seconds=60`. `leftover_timeout_s` is not set, so the leftover check uses 10 s. `free_floor_gib` serves both the probe's disk decline and the cleanup's keep-or-remove test. A repository needs a `linux_only.<repo>` key, even empty, before it runs on the Mac. `MAC_TEST_CONFIG` selects another file.

## Invariants

- Mac trouble other than the tests' own result permits local fallback only when natedev is eligible and not held; otherwise routing waits. The reach and resource probes are bounded by their timeout.
- Before CI either machine answers a test. A Mac pass writes a pass record marked `host=mac`, which every call but `--local` reuses.
- A printed `mac_exit=<n>` followed by nothing but `mac_cleanup=` markers is the result of a finished test even if ssh failed afterward. With no such line, a run is lost; reach and control checks require their own `rc=<n>` answer.
- Tests run nothing on the Mac: stand-in `ssh`, `rsync`, `gh` on `PATH`, state under `MAC_TEST_STATE_DIR`, config under `MAC_TEST_CONFIG`. Tests set these variables for a checkout's scripts; installed scripts refuse to run with either set. A test reaching the footer sets `MAC_TEST_STATE_DIR` too.
- On the Mac the feature writes and removes only under `~/.local/state/mac-test/`. The cleanup removes nothing but the mirror's `target` folder, and only below `free_floor_gib`; `/tmp` and the user's clones are never touched. Only step records are written there; call records are written on natedev only.
- The claim files live on natedev. Process-bound claims expire when their owner ends. Remote claims require release.
  The watcher releases a kept claim when its saved mirror is empty and its exact fence settles, or when the Mac idle rule permits release.
- One live claim can hold a repository. A whole-Mac claim in `run.json` excludes all other claims.
  A block refuses claims from other sessions.
- A copy never replaces a copy, and a Mac-only (non-copy) run never gives way; only a live copy claim gives way, and only to a non-copy claim.
- A copy win stops natedev only through a live group leader whose start time matches; a step that already ended keeps its result.

## Calibration and gotchas

- The mirror copy and the tracked script copy use `copies.RSYNC_RECEIVER_WRITE_TIME_FLAGS` (`rsync -rlpc`).
  Checksums skip files with equal content and keep their destination modification times.
  Changed files get the receiver write time, so Cargo detects changed source even when its source modification time is old.
  A change to the source modification time alone causes no transfer.
- GitHub lists hana's macOS job only after `macOS: Runner Availability` completes, and lists it at once as completed/skipped while the switch is off. A pending block therefore also waits on queued runs that have not reached that job.
- A lost run whose cleanup cannot confirm the end keeps its claim. A remote claimant takes it over with exit 13 and checks the stored host. Remaining work declines `busy`; an unreadable host or failed check declines `unreachable` and starts the unreachable backoff. Both keep the claim. Stderr says `mac_test: <N> leftover processes of a lost run are still on the Mac; it stays busy until they end`, or `mac_test: could not confirm that a lost run's leftovers ended: <reason>; the Mac stays busy`.
- A non-interactive bash starts an `&` child with `SIGINT` ignored, so a forwarded INT is lost until `offload.py run` installs its own handlers. `verify.sh` therefore forwards INT as TERM. A terminal interrupt reaches the runner twice: INT from the process group and TERM from the forward.
- The message and session lookup run outside `state.lock`; send.py can take 40 s.
- The activity check counts any of the user's processes whose executable path contains `/target/`, so an app the user runs from a build folder on the Mac keeps a claim alive. The user's shells under the Mac's clones do not count.
- Offloaded Mac nextest rows in the build log have an empty `repo`; filter them with `coalesce(repo,'')=''` and `host <> 'natedev'`. The log carries two Mac host names (`Mac` and `Nates-MacBook-Pro-2023`), so Mac selection is `host <> 'natedev'`, never one name.
- A `gh` set that times out after GitHub applied it reads `still_on`, then `off_before` at the next settle. Turning the switch back on after `off_unconfirmed` can undo a hand-made switch-off in between.
- `world_panel_words_match_drawn_pixels` fails only on the Mac, cause not found; it is excluded there, so only natedev runs and CI cover it.
- `basedpyright scripts/mac_test` exits 0 when clean. Its summary reads `0 errors, 0 warnings, 0 notes`.

## Why

A remote result ends the step without a local confirmation run.
CI checks platform differences.
`--local` selects tests that require Linux.

A repository claim protects its mirror and build output from concurrent runs of that repository.
Different repositories can share the Mac because their mirrors and build output differ.
The leftover check uses the same repository scope because it stops matching processes.

The CI block prevents local offload from competing with CI on the Mac.
An idle claim watcher releases unused claims and tells the holder.
An unreachable Mac keeps a remote claim until cleanup can confirm that its work ended.

The disk floor preserves useful build output while enough free space remains.
A Mac copy can finish a local compiling test earlier.
A new non-copy job can preempt that copy to preserve normal routing priority.

All block writers hold their coordination lock.
The footer reads the block file directly, without a lock, GitHub request, or message.

## Registered remote mirrors

`OffloadRequest` requires the copy owner and holder session.
It also carries the repository scope.
`request_from_arguments` selects the holder through `owning_session.owning_session()` before the shell session fallback.
`request_mirror` uses this supplied session without another lookup.
Busy refusals can name the unit director's session.

`offload.py` requests Mac, Linux, and AWS mirrors through `copies.py`.
Each SSH target permits one claim per repository.
Native Mac and mac-linux work share this limit.
On Linux targets, the sender publishes mirror ownership after admission succeeds.
Failed admission preserves the mirror owner.
The reader keeps the saved file and fence for each old numbered claim.
An old live claim holds the repository.
When a local owner ends, the reader releases its local claim.
For each remote claim, the runner checks the saved mirror before release.
New Linux claims use the first mirror.
The runner takes the saved assignment's mirror lease before it adopts a Linux reservation.

The Terminal launcher enters the numbered copy before it checks its per-run token.
Cleanup removes the token before `stop-checkout` scans the copy.
The copy stays kept if `stop-checkout` cannot confirm it is clear.
The Mac wrapper removes any old `status` file before it starts `copies.py run`.
The file path is `<scratch root>/mac-smoke-<episode>/status` on the Mac.
If SSH leaves the folder behind, the hourly scratch sweep lists it for vetted manual removal after its default lifetime.
The test and cleanup read stdin from `/dev/null`.
After remote cleanup, the worker reports readiness and holds the copy until it receives a newline after local cleanup.
SSH EOF before the newline stops active work and prevents a zero exit code.
The worker uses the run's `mac_run_status_grace_seconds` limit from the Mac's `production.conf` for the release wait.
If this limit expires, the worker returns a failure and lets the copy lease close.
After `copies.py run` returns, the wrapper writes its exit code to a temporary file and renames it to `status`.
The exit code includes any copy lease close failure.
After the first SSH call ends, the queue reads the status and removes its scratch folder through a separate SSH call.
The queue reads the file only after readiness and requires the first SSH call to finish without a connection error.
The second SSH call uses `probe_timeout_s` from `config/mac_test.conf`.
The queue returns 255 and keeps the run record if the connection fails or the read cannot return a valid status.
For an old scratch run, the callback confirms checkout clearance before it ends the task and removes both registry rows.
The queue removes the old run record and receipt after successful cleanup.

[Gate checkouts](gate-checkouts.md#remote-mirrors) defines the mirror metadata, lock order, recovery, process scans, registry commands, and probe behavior.
Candidate leases precede the shared scan, so a process that starts before lease acquisition protects its mirror.
The scan accepts lsof output only with exit status 0 or 1.

## Documentation copies

`verify.sh doc` calls `copies.py hold` without a staged-tree argument.
`hold` computes the staged tree and passes it to `copies.standing` through `StagedSource(tree)`.
It prints the tree identity and checkout path on separate lines.
`verify.sh` reads both lines.
The private index folder uses the configured `prefixes.copies-index` name through `build_folders.temporary_build_folder`.
The copy owner holds the lease until the verify shell closes its input descriptor.
Both routes use the staged snapshot.
Local rustdoc keeps the original target directory. Remote rustdoc uses its mirror's build output.
The source index remains unchanged.
A pass record still requires the original staged tree identity after the step.
The token boundary, local repair rule, and step records remain in verify.sh.

### Failed probes and recovery

A failed reach writes `failed_probe` in the target state folder.
The record has no expiry time.
A successful probe removes the mark and retry backoff if both records predate the probe.
An unresolved startup or an interrupted target keeps the record unchanged.

A tool version mismatch writes `version-decline-<repository>` in the same folder.
This record stops only that repository.
Only a matching run removes it, on Linux or macOS.
A mismatch writes no target backoff.

`free-now` answers `bad` when either record stops an otherwise free target.
The answers `off`, `backoff`, `blocked`, `state` and `busy` keep their order.
A waiting step cannot reserve a bad target.
The first routing pass can use `reserve --new-step`.
A pinned step starts no recovery probe and takes no reservation during eligibility.
Its normal run probes the target and clears a successful target mark.

Each step's first eligibility read can start one detached recovery probe per target.
The probe takes the normal claim, copies tracked scripts, copies the repository tree, and probes the target.
It releases the claim after the probe or a stop signal.
It keeps repository version records unchanged.
A busy claim skips recovery.
A second step does not duplicate a running recovery probe.
The step routes without waiting for recovery.
Repeated reads from a waiting step start no recovery probe.

Target status reports a failed probe as unreachable.
The reach error shows only the last nonempty stderr line.
A fixed target returns when the next new step probes it.
Existing waits keep their place on natedev until then.

### Disk floor recovery

`ensure_mac_disk_floor` returns `MacDiskReady`, `SweepShort`, or `SweepFailed`.
Below the required floor, it runs one shared floor sweep.
It reads free space again after a successful sweep.
The per-host `floor-sweep-<host>.lock` serializes sweeps.
A direct caller that finds a running sweep waits for its result.
The sweep uses a dedicated SSH terminal. The terminal sends `SIGHUP` when the SSH connection closes.
Natedev uses the full `floor_sweep_timeout_s` for SSH.
Natedev reserves the smaller of half that timeout and the sum of its `connect_timeout_s` and `probe_timeout_s`.
It sends the resulting deadline as a Unix time when it starts SSH.
The Mac supervisor uses the time that remains at startup. It skips the sweep if the deadline has passed.
This deadline relies on the two clocks agreeing. The reserve estimates time for SSH setup and the reply.
Slow setup, clock differences, or a transport failure can prevent natedev from receiving the result in time.
On disconnect or timeout, the supervisor stops the sweep process group and waits for the sweep process to exit.
The supervisor sends a cleanup record after the worker exits. It also sends one if the deadline passed before startup.
If `SIGKILL` ends the worker, the supervisor exits with status 137 and sends no record.
Natedev reports an unsuccessful sweep without that record as unconfirmed cleanup.
Natedev also reports unconfirmed cleanup if SSH times out, loses the connection, or leaves no result.
An SSH retry waits for `floor.lock` within its deadline before it reads mirror leases and claims.
A standalone sweep skips an occupied floor lock and returns status 0.
The sweep publishes a complete review file with an atomic rename.
A later publication removes temporary files from writers that have exited.
Both scans report a temporary file removal error and continue with publication, including when the list path stays the same.
If the rename succeeds and the directory sync fails, a later publication by the same list keeps that path. A new SSH attempt starts a new list with a new path.
Floor rows require manual review. The removal command refuses those rows.
The evidence worker reads disk space and saved mirror processes in one SSH command per Mac probe cycle.
The same command reports setup for each required mirror.
Each step requires a successful setup result for its mirror.
The worker reads local claims again after SSH.
It attaches process evidence only to claims that match the records from before SSH.
The keys that share the reading have the same observation time.
The worker checks repository policy, session, and claim scope for each key.
The worker reports pending disk recovery while a separate thread runs the sweep.
The Mac stays excluded during recovery even when a probe reports enough free space.
When the sweep ends, the worker probes all subscribed Mac keys again.
The sweep holds no admission or claim lock while it lists output.
`floor_claims.serve()` reads claims and mirror leases under `state.lock`.
It builds the reply under the lock and prints after the lock closes.
It exits without reading stdin.
`read_claims()` closes both reader processes before it returns a `ClaimSnapshot`.
A lost SSH session cannot retain the state lock while the sweep runs.
An unavailable or unreadable claim reply protects all output.

`mirror_sweep_leases()` reports acquired leases separately from busy mirrors.
Discovery still walks all three configured roots.
The alert still measures folders under those roots when a disk alert is due.
Mirror leases and the claim snapshot protect busy mirrors during candidate selection.
The sweep holds acquired leases while it lists output.
For output outside those leases, it reads claims again before it adds each group to the list.
A new claim or an unreadable reply keeps that output.
Cargo locks, scratch busy locks, and process checks retain their existing roles.

| Path class | Protection during listing or removal |
| --- | --- |
| Mirror build units, orphan files, and whole targets | The sweep holds the Mac-side mirror lease while it lists output. |
| Registered mirror clones outside the mirror root | The sweep holds their registered Mac-side mirror leases while it lists output. |
| Marked gate caches and other output outside acquired leases | The sweep reads claims again before listing each group and holds existing output locks. |
| `mirror-remove` payload, metadata, and removal authority | `local_mirror_lease` covers the operation; the record lock serializes metadata changes. |
| A registered task clone's parent folder | An outside mirror lease survives parent removal and blocks concurrent `mirror-use`. No parent busy lock applies. |
| `mirror-receive` cleanup of interrupted removal | The mirror lease and record lock cover the old payload and metadata. |
| Natedev floor output | Existing build holds, mirror leases, process checks, scratch locks, and Cargo locks apply. |
`mirror_root.py` reads the one `mirror_root` setting for offload and sweep.
`free_now` reads local records only and starts no sweep or SSH request.

`local_mirror_lease` first takes an outside lease, then the existing sibling lease.
The outside file uses the mirror path hash and sits beside the task parent.
After acquisition, the lease compares the open file inode with the path inode.
A missing path or a changed inode causes the existing busy refusal.
When task removal deletes the parent, lease cleanup deletes the outside file before it closes either lease.
Both lease descriptors pass to remote commands.
Task removal can delete the sibling lease and record lock while the outside lease remains locked.
The sibling lease path stays unchanged, so a live lease from the old code still blocks removal.

A disk decline remains retryable through the local queue leave hook.
The step record retains disk diagnostics for idle-alert evidence.
Permanent exclusions stop remote retries.
Every claim and command still checks AWS spending rules.

### Queue claim handoff

Converted callers request placement before they claim a provider scope.
`place-step --once` writes an `Assignment` or `LocalPlacementGranted` file.
A wait retains the episode, original queue time, run place, and priority.
Token readiness reports remain separate from fixed placement constraints.
No placement wait holds the cargo token; a local grant takes it.
A remote selection releases the unused continuation claim.

The adapter claims the assignment's target and scope under the provider lock.
Distinct known Mac repository scopes can coexist.
A whole-Mac scope overlaps every repository scope.
A definite refusal reports `ClaimFailure` before the queue decides another attempt.
An uncertain response requires inspection of the same exact identity.
It cannot start another claim or choose another destination.

`wait_claim_identity` identifies the current claim owner.
Adoption preserves the claim identity and updates the local holder under the target lock.
`submitting_session` identifies that current holder's session.
`holder_session` preserves the original job evidence.
Strict session equality prevents an unrelated session from adopting the claim.
`ClaimHolderIdentity` names the offload child.
`SubmittingOwner` remains the verify coordinator.
The service checks both lifetimes and their process-tree relationship.

The adapter consumes permission after adoption and before remote execution.
Only `FirstAuthorization` permits one execution.
Repeated consumption returns status across request IDs and service restarts.
A lost first response authorizes zero executions for that attempt.
A status response cannot recover authorization.

The adapter resolves pending claim and start children before it reports a settled dispatch path.
Provider cleanup retains the exact claim until departure is confirmed.
Owner death, SSH exit, timeout, and an empty scan cannot bypass that barrier.
Cleanup takeover grants no launch authority.
The service reconstructs uncertain execution rows from provider records and background observations.
It invokes no destructive cleanup.
The provider remains the physical release authority.

A completed command remains terminal under the caller policy.
A settled prelaunch refusal can produce a new attempt with the original queue position.
Busy, blocked, and disk outcomes use normal polling.
Other temporary Mac outcomes delay that target for 180 seconds.
Permanent declines remove only that target from further retries.

The views show each execution's host.
Unavailable process identity remains uncertain.
Unknown remote memory remains unknown and creates no local memory charge.

## Mirror refusal times

One `mirror_busy_backoff_s` key in `mac_test.conf` serves both Mac and AWS targets.
Each target stores `mirror-busy-<repository>.until` beside its unreachable backoff record.
A mirror busy refusal during copy writes this time.
If the mirror lease is busy, the caller also writes this time.
A `mirror-use` busy refusal after copy becomes `BusyMacDecline`, without a lost run.
A successful copy removes the repository's refusal time.
`free-now` reads this local record without SSH or a lock.
A live time answers `busy` only for its target and repository.
The claim and mirror lease still make the final decision under their locks.
Read access alone does not make a mirror busy.
A working directory, a mapped file, or a file open for writing still makes it busy.
Unavailable scans and unreadable lease groups keep their protection.
